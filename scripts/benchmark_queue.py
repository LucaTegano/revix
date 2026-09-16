#!/usr/bin/env python3
"""Measure enqueue-to-claim dwell time on the Postgres-native queue.

Two regimes are reported, because they answer different questions and only one
of them is "queue latency":

  cold burst  - N jobs are enqueued, *then* W workers start and drain them.
                Dominated by worker startup and connection-pool warmup, so it
                measures cold-start drain, not queue behaviour.

  warm steady - W workers are already polling an empty queue; jobs are then
                enqueued and each one's dwell is enqueue -> claim. This is the
                latency a running deployment actually exhibits.

Inference is not simulated. A claimed job is released immediately, so the number
is queue dwell and nothing else.

  docker compose up -d db
  python scripts/benchmark_queue.py --jobs 1000 --workers 100
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
import time
from datetime import UTC
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from app.config import settings  # noqa: E402
from app.services.db.core import db_core  # noqa: E402
from app.services.db.queue import queue_repo  # noqa: E402

logging.basicConfig(level=logging.WARNING)
BENCH_REPO = "benchmark/queue"


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(int(len(ordered) * p), len(ordered) - 1)
    return ordered[idx]


async def _reset() -> None:
    pool = db_core.get_pool()
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM jobs WHERE repo_full_name = %s", (BENCH_REPO,))


async def _enqueue(n: int, tag: str) -> float:
    async def one(i: int) -> None:
        await queue_repo.enqueue_if_new(
            sha=f"{tag}_{int(time.time() * 1e6)}_{i}",
            repo=BENCH_REPO,
            pull_number=i,
            installation_id=123,
        )

    begin = time.time()
    for start in range(0, n, 500):
        await asyncio.gather(*(one(i) for i in range(start, min(start + 500, n))))
    return time.time() - begin


async def _drain(
    worker_id: int, dwells: list[float], target: int, claimed: list[int], stop: asyncio.Event
) -> None:
    """Claims jobs and records enqueue->claim dwell.

    Claimed jobs are left in `processing` rather than released: releasing would
    return them to `pending` and they would be claimed again, counting one job's
    dwell several times. They are deleted by the reset between regimes.
    """
    while not stop.is_set() and len(claimed) < target:
        job = await queue_repo.claim_job(f"bench-{worker_id}")
        if job is None:
            await asyncio.sleep(0.005)
            continue
        created = job["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        dwells.append(time.time() - created.timestamp())
        claimed.append(1)


async def cold_burst(jobs: int, workers: int) -> tuple[list[float], float]:
    await _reset()
    enqueue_s = await _enqueue(jobs, "cold")
    print(f"    (enqueue of {jobs} jobs took {enqueue_s * 1000:.0f}ms)", flush=True)
    dwells: list[float] = []
    claimed: list[int] = []
    stop = asyncio.Event()
    drain_start = time.time()
    tasks = [asyncio.create_task(_drain(w, dwells, jobs, claimed, stop)) for w in range(workers)]
    deadline = time.time() + 120
    while len(claimed) < jobs and time.time() < deadline:
        await asyncio.sleep(0.05)
    drain_s = time.time() - drain_start
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    return dwells, drain_s


async def warm_steady(jobs: int, workers: int) -> tuple[list[float], float]:
    await _reset()
    dwells: list[float] = []
    claimed: list[int] = []
    stop = asyncio.Event()

    # Workers poll an empty queue first, so pools are connected and loops hot.
    tasks = [asyncio.create_task(_drain(w, dwells, jobs, claimed, stop)) for w in range(workers)]
    await asyncio.sleep(2.0)

    drain_start = time.time()
    enqueue_s = await _enqueue(jobs, "warm")
    print(f"    (enqueue of {jobs} jobs took {enqueue_s * 1000:.0f}ms)", flush=True)
    deadline = time.time() + 180
    while len(claimed) < jobs and time.time() < deadline:
        await asyncio.sleep(0.05)
    drain_s = time.time() - drain_start
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    return dwells, drain_s


def report(label: str, dwells: list[float], note: str, drain_s: float = 0.0) -> None:
    ms = [d * 1000 for d in dwells]
    print(f"\n  {label}  (n={len(ms)})")
    print(f"    {note}")
    print(
        f"    p50 {pct(ms, 0.50):8.1f}ms   p95 {pct(ms, 0.95):8.1f}ms   "
        f"p99 {pct(ms, 0.99):8.1f}ms   max {max(ms):8.1f}ms"
        if ms
        else "    no samples"
    )
    if ms:
        print(f"    mean {statistics.fmean(ms):7.1f}ms")
    if drain_s > 0 and ms:
        print(
            f"    drained {len(ms)} jobs in {drain_s * 1000:.0f}ms -> {len(ms) / drain_s:,.0f} claims/s"
        )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=100)
    args = ap.parse_args()

    await db_core.connect()
    pool_max = settings.db_pool_max_size
    print(f"  connection pool max : {pool_max}")
    if pool_max < args.workers:
        print(
            f"  NOTE: pool ({pool_max}) is smaller than worker loops ({args.workers}),\n"
            f"        so this run measures pool contention as much as queue behaviour.\n"
            f"        Re-run with DB_POOL_MIN_SIZE={args.workers + 20} to size it up."
        )
    try:
        print("=" * 68)
        print("  Enqueue-to-claim dwell  |  Postgres queue, SELECT ... SKIP LOCKED")
        print("=" * 68)
        print(f"  jobs {args.jobs}   concurrent worker loops {args.workers}   inference: none")

        print("  running cold burst...", flush=True)
        cold, cold_s = await cold_burst(args.jobs, args.workers)
        report(
            "cold burst",
            cold,
            "queue pre-loaded, then workers start - includes pool warmup",
            cold_s,
        )

        print("  running warm steady state...", flush=True)
        warm, warm_s = await warm_steady(args.jobs, args.workers)
        report(
            "warm steady state",
            warm,
            "workers already polling - this is queue dwell alone",
            warm_s,
        )
        print("\n" + "=" * 68)
        print(
            "  Quote the warm figure as queue latency. The cold figure is a\n"
            "  cold-start measurement and is dominated by worker startup."
        )
        print("=" * 68 + "\n")
        await _reset()
    finally:
        await db_core.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
