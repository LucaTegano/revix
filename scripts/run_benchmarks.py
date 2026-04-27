import statistics
import time
from pathlib import Path

from app.services.ai import RepoIndexer

REPOS = [
    ("revix", Path("fixtures/revix"), [".py"]),
    ("django", Path("fixtures/django"), [".py"]),
    ("flask", Path("fixtures/flask"), [".py"]),
    ("fastapi", Path("fixtures/fastapi"), [".py"]),
    ("express", Path("fixtures/express"), [".js"]),
    ("golang", Path("fixtures/golang"), [".go"]),
]


def benchmark_repo(name, path, extensions):
    files = []
    for ext in extensions:
        files.extend(path.rglob(f"*{ext}"))

    if not files:
        return None

    latencies = []
    symbol_count = 0
    ref_count = 0
    indexer = RepoIndexer()

    # Sample up to 1000 files to keep benchmark time reasonable
    import random

    if len(files) > 1000:
        files = random.sample(files, 1000)

    for file_path in files:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
            start = time.perf_counter()
            symbols, refs = indexer.index_file(str(file_path), source)
            elapsed_ms = (time.perf_counter() - start) * 1000

            latencies.append(elapsed_ms)
            symbol_count += len(symbols)
            ref_count += len(refs)
        except Exception:
            continue

    if not latencies:
        return None

    return {
        "repo": name,
        "files": len(latencies),
        "total_symbols": symbol_count,
        "total_refs": ref_count,
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(statistics.quantiles(latencies, n=20)[18], 2),
        "p99_ms": round(statistics.quantiles(latencies, n=100)[98], 2),
        "max_ms": round(max(latencies), 2),
    }


if __name__ == "__main__":
    print(
        f"{'Repo':<12} {'Files':>6} {'Symbols':>8} {'Refs':>8} "
        f"{'p50ms':>7} {'p95ms':>7} {'p99ms':>7} {'maxms':>7}"
    )
    print("-" * 75)

    for name, path, exts in REPOS:
        r = benchmark_repo(name, path, exts)
        if r:
            print(
                f"{r['repo']:<12} {r['files']:>6} {r['total_symbols']:>8} "
                f"{r['total_refs']:>8} {r['p50_ms']:>7} {r['p95_ms']:>7} "
                f"{r['p99_ms']:>7} {r['max_ms']:>7}"
            )
