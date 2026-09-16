#!/usr/bin/env python3
"""Measure the prompt-token cost of diff-scoped chunking against a full-file baseline.

Corpus is real commits from a local git repository, so every sample is a diff a
human actually wrote. For each changed file the script builds the exact messages
the review pipeline would send and counts tokens with LiteLLM's tokenizer for the
configured model.

Both arms are costed end-to-end, including per-call overhead:

    baseline  = 1 call  · (system + intent + whole file)
    scoped    = N calls · (system + intent + chunk_i)

This matters: scoping produces more calls, and each one repeats the system prompt
and PR intent. Comparing chunk size alone would flatter the result.

  python scripts/benchmark_tokens.py --commits 40
  python scripts/benchmark_tokens.py --commits 40 --live 15   # validate tokenizer

`--live` re-sends a subsample through the real API and compares the tokenizer's
count with the provider's reported `usage.prompt_tokens`.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from litellm import token_counter  # noqa: E402

from app.services.ai import AIService, RepoIndexer, parse_changed_lines  # noqa: E402

SUPPORTED = (".py", ".js", ".go")
INTENT = (
    "PR INTENT:\nRefactor and bug-fix pass across the touched modules. "
    "Review for correctness, security and performance regressions."
)


def sh(*args: str, cwd: str) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False).stdout


@dataclass
class Sample:
    commit: str
    path: str
    baseline_tokens: int
    scoped_tokens: int
    chunks: int
    touched_share: float = 0.0
    baseline_calls: int = 1


@dataclass
class Totals:
    samples: list[Sample] = field(default_factory=list)

    @property
    def baseline(self) -> int:
        return sum(s.baseline_tokens for s in self.samples)

    @property
    def scoped(self) -> int:
        return sum(s.scoped_tokens for s in self.samples)


def collect(repo: str, max_commits: int, max_files: int) -> list[tuple[str, str, str, str]]:
    """Returns (commit, path, full_source_at_commit, patch) tuples."""
    revs = sh("git", "rev-list", f"-{max_commits}", "HEAD", cwd=repo).split()
    out: list[tuple[str, str, str, str]] = []
    for rev in revs:
        names = sh("git", "diff-tree", "--no-commit-id", "--name-only", "-r", rev, cwd=repo).split()
        picked = 0
        for path in names:
            if not path.endswith(SUPPORTED) or picked >= max_files:
                continue
            source = sh("git", "show", f"{rev}:{path}", cwd=repo)
            if not source.strip():
                continue  # deleted or empty at this rev
            patch = sh("git", "diff", "--unified=3", f"{rev}^", rev, "--", path, cwd=repo)
            if "@@" not in patch:
                continue  # first commit / binary / pure rename
            out.append((rev[:8], path, source, patch))
            picked += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--commits", type=int, default=40)
    ap.add_argument("--max-files-per-commit", type=int, default=4)
    ap.add_argument("--live", type=int, default=0, help="validate tokenizer on N samples")
    ap.add_argument(
        "--call-overhead",
        type=int,
        default=169,
        help="fixed prompt tokens the provider adds per call (measured: see docs/TOKENS.md)",
    )
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    model = os.environ.get("AI_MODEL_MAP", "gpt-4o")
    svc = AIService.__new__(AIService)
    svc.indexer = RepoIndexer()
    svc.MAX_CHUNK_TOKENS = AIService.MAX_CHUNK_TOKENS
    system = AIService.REVIEW_AGENT_PROMPT

    def cost(chunk: str) -> int:
        """Billed prompt tokens for one agent call carrying `chunk`.

        Includes the provider's fixed per-call overhead, so a chunking strategy
        that trades one big call for several small ones is charged for it.
        """
        return args.call_overhead + token_counter(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"{INTENT}\n\nCODE:\n{chunk}"},
            ],
        )

    corpus = collect(args.repo, args.commits, args.max_files_per_commit)
    if not corpus:
        print("No usable commits found.", file=sys.stderr)
        return 1

    totals = Totals()
    unscoped = 0  # files where the diff touched everything anyway

    for commit, path, source, patch in corpus:
        chunks = svc._chunk_by_ast(path, source, patch=patch)
        if not chunks:
            continue
        baseline = cost(f"FILE: {path}\n{source}")
        scoped = sum(cost(c) for c in chunks)
        if scoped >= baseline:
            unscoped += 1
        file_lines = max(source.count("\n"), 1)
        share = len(parse_changed_lines(patch)) / file_lines
        totals.samples.append(
            Sample(commit, path, baseline, scoped, len(chunks), touched_share=share)
        )

    n = len(totals.samples)
    per_file = [100 * (1 - s.scoped_tokens / s.baseline_tokens) for s in totals.samples]
    agg = 100 * (1 - totals.scoped / totals.baseline)

    print(f"\n{'=' * 68}")
    print("  Prompt tokens: diff-scoped chunking vs full-file baseline")
    print(f"{'=' * 68}")
    print(f"  model (tokenizer)   : {model}")
    print(f"  per-call overhead   : {args.call_overhead} tokens (measured against the live API)")
    print(f"  files measured      : {n}  (from {args.commits} commits)")
    print(f"  baseline total      : {totals.baseline:>10,} prompt tokens")
    print(f"  scoped total        : {totals.scoped:>10,} prompt tokens")
    print(f"  aggregate reduction : {agg:>9.1f}%")
    print()
    print(f"  per-file median     : {statistics.median(per_file):>9.1f}%")
    print(f"  per-file mean       : {statistics.fmean(per_file):>9.1f}%")
    print(f"  best / worst        : {max(per_file):.1f}% / {min(per_file):.1f}%")
    print(f"  files where scoping cost MORE than baseline: {unscoped}/{n}")
    print(f"  mean calls per file : {statistics.fmean([s.chunks for s in totals.samples]):.2f}")
    print(f"{'=' * 68}\n")

    print("  Reduction by share of file the diff touches:")
    print(f"    {'touched':<14}{'files':>6}{'baseline tok':>14}{'scoped tok':>12}{'reduction':>11}")
    buckets = [
        ("< 5%", 0.0, 0.05),
        ("5-15%", 0.05, 0.15),
        ("15-40%", 0.15, 0.40),
        (">= 40%", 0.40, 1e9),
    ]
    for label, lo, hi in buckets:
        rows = [s for s in totals.samples if lo <= s.touched_share < hi]
        if not rows:
            continue
        b = sum(r.baseline_tokens for r in rows)
        sc = sum(r.scoped_tokens for r in rows)
        print(f"    {label:<14}{len(rows):>6}{b:>14,}{sc:>12,}{100 * (1 - sc / b):>10.1f}%")
    print()

    worst = sorted(totals.samples, key=lambda s: s.scoped_tokens - s.baseline_tokens)[-5:]
    print("  Worst 5 (scoping helps least — per-call overhead dominates):")
    for s in reversed(worst):
        pct = 100 * (1 - s.scoped_tokens / s.baseline_tokens)
        print(f"    {pct:>7.1f}%  {s.chunks:>2} chunks  {s.path} @ {s.commit}")
    print()

    if args.live:
        validate_live(totals, corpus, svc, model, system, args.live)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "model": model,
                    "files": n,
                    "baseline_tokens": totals.baseline,
                    "scoped_tokens": totals.scoped,
                    "aggregate_reduction_pct": round(agg, 2),
                    "per_file_median_pct": round(statistics.median(per_file), 2),
                    "files_scoping_cost_more": unscoped,
                    "samples": [vars(s) for s in totals.samples],
                },
                indent=2,
            )
        )
        print(f"  wrote {args.json_out}\n")
    return 0


def validate_live(
    totals: Totals, corpus: list, svc: AIService, model: str, system: str, n: int
) -> None:
    """Compare the tokenizer's count against the provider's reported usage."""
    import litellm

    key = os.environ.get("AI_API_KEY")
    if not key:
        print("  [live] AI_API_KEY not set; skipping validation.\n")
        return

    print(f"  Validating tokenizer against live usage.prompt_tokens (n={n})...")
    deltas: list[float] = []
    for _commit, path, source, patch in corpus[:n]:
        chunks = svc._chunk_by_ast(path, source, patch=patch)
        if not chunks:
            continue
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{INTENT}\n\nCODE:\n{chunks[0]}"},
        ]
        counted = token_counter(model=model, messages=msgs)
        try:
            resp = litellm.completion(model=model, api_key=key, messages=msgs, max_tokens=1)
            actual = resp.usage.prompt_tokens
        except Exception as exc:  # noqa: BLE001
            print(f"    [live] {path}: call failed ({type(exc).__name__}); skipped")
            continue
        delta = 100 * (actual - counted) / actual
        deltas.append(delta)
        print(f"    counted {counted:>6,} | actual {actual:>6,} | delta {delta:>+6.1f}%  {path}")

    if deltas:
        print(
            f"\n  tokenizer vs provider: mean delta {statistics.fmean(deltas):+.1f}%, "
            f"max |delta| {max(abs(d) for d in deltas):.1f}% over {len(deltas)} calls\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
