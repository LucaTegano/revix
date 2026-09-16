# Prompt tokens: measured, not modelled

**Harness:** [`scripts/benchmark_tokens.py`](../scripts/benchmark_tokens.py)

```bash
git clone --depth 300 https://github.com/psf/requests.git /tmp/requests
python scripts/benchmark_tokens.py --repo /tmp/requests --commits 250
```

## What is being compared

Every agent call carries a system prompt, the PR intent, and a code payload.
Two strategies are costed end-to-end over the same set of real commits:

| Arm | Payload | Calls |
| :--- | :--- | ---: |
| **Baseline** | the whole file, as the pipeline sent it before | 1 |
| **Scoped** | only the syntax nodes the diff touches | *n* |

Both arms are charged the provider's fixed per-call overhead, so a strategy
that trades one large call for several small ones pays for that. Without this,
chunking looks better than it bills.

## Results

Corpora are the real commit histories of two mature Python projects. Each
sample is one file changed by one commit — a diff a human actually wrote.

| Corpus | Files | Baseline | Scoped | Reduction |
| :--- | ---: | ---: | ---: | ---: |
| `psf/requests` (250 commits) | 128 | 850,758 | 394,756 | **53.6%** |
| `pallets/flask` (250 commits) | 134 | 847,215 | 467,285 | **44.8%** |
| **Combined** | **262** | **1,697,973** | **862,041** | **49.2%** |

Scoping never costs more than the baseline: the worst file in either corpus is
0.0% (break-even), because touched nodes are packed up to the chunk budget
rather than emitted one call per node.

### The saving is a function of diff size

This is the number's main dependency, and it is large:

| Share of file touched | Files | Reduction |
| :--- | ---: | ---: |
| < 5% | 97 | 55.0% |
| 5–15% | 10 | 19.0% |
| 15–40% | 17 | 34.0% |
| ≥ 40% | 4 | 4.1% |

*(`psf/requests`; `flask` follows the same shape.)*

A review bot earns its saving on surgical diffs against large files — which is
most PR traffic — and earns nothing on a rewrite. Quoting a single headline
number without this table overstates the result for large diffs.

### The floor: this repository's own history

Run against Revix's own commits the reduction is **19.4%** over 55 files. That
history is dominated by `refactor: unify ...` commits that rewrite most of a
file, so nearly every node is in scope and there is little to exclude. It is
reported here because it is the honest worst case, not because it is typical.

## Tokenizer validation

The corpus figures come from LiteLLM's `token_counter`. To confirm the counter
tracks what the provider actually bills, a subsample is re-sent live and
compared against the returned `usage.prompt_tokens`:

```bash
python scripts/benchmark_tokens.py --repo /tmp/requests --commits 30 --live 12
```

Over 12 live calls the counter was consistently **low by a near-constant
offset** (median 169 tokens) rather than by a proportion:

| counted | actual | offset |
| ---: | ---: | ---: |
| 155 | 312 | +157 |
| 909 | 1,068 | +159 |
| 4,096 | 4,281 | +185 |
| 18,609 | 19,138 | +529 |

That offset is provider scaffolding added once per call, not a tokenizer error
on the payload. It is measured separately (probe payloads from 1 to 2,000
characters return the same ~169-token gap) and added to **both** arms via
`--call-overhead`, which is why the headline is 49.2% and not the 51.5% the
raw payload counts alone would suggest.

## Scope and limitations

- Both corpora are Python. Tree-sitter grammars for JS and Go are wired in but
  the reduction is not measured for them.
- The comparison is prompt tokens only. It says nothing about review quality:
  a smaller prompt that omits a caller the agent needed is a worse review, and
  this harness cannot detect that.
- The baseline is the whole file, which is what this pipeline sent before the
  diff-scoping fix. It is not a claim about what other review tools send.
- `--call-overhead` is measured against one provider (OpenRouter) and one
  model. A different endpoint will have a different constant.

## Why the earlier figure was withdrawn

A previous revision of the README claimed roughly 87% token reduction and a 3x
pipeline speedup. Both came from `scripts/benchmark_ai_pipeline.py`, which has
been deleted rather than fixed:

- Its token figure estimated tokens as `len(text) // 4` against a hypothetical
  full-file baseline that was never actually sent. A model of a saving, not a
  measurement of one.
- Its "speedup" compared `asyncio.gather` against a sequential loop over
  `asyncio.sleep()` with hardcoded latency constants. That comparison is
  tautologically true for any values chosen and says nothing about the
  pipeline: no inference ran, and the real system's latency is dominated by the
  provider, not by the scheduling.

`scripts/benchmark_tokens.py` replaces the token half against real commits and
a real tokenizer. The concurrency claim is simply not made — the fan-out is
visible in `analyze_diff()` as two `asyncio.gather` stages, but its end-to-end
benefit has not been measured under real inference.

It was also unreachable in production: the chunker was being handed GitHub's
`patch` field, which is a unified diff rather than parseable source. See
[`docs/CHUNKING.md`](CHUNKING.md) for what that broke and how it was fixed.
