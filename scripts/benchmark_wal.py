"""Measure the WAL volume eliminated by the UNLOGGED heartbeat table.

UNLOGGED tables skip write-ahead logging entirely, so heartbeat traffic
generates no WAL, no replication stream, and no archive volume. The cost is
that the table does not survive a crash — acceptable here because heartbeats
are ephemeral liveness signals that the reconciliation loop rebuilds.

Method:
  1. CHECKPOINT, read pg_current_wal_lsn().
  2. Run a fixed workload.
  3. Read pg_current_wal_lsn() again; diff with pg_wal_lsn_diff().

Three workloads are measured:
  - heartbeat upserts against a LOGGED table
  - the same upserts against an UNLOGGED table
  - one full job lifecycle (enqueue, claim, finalize + review record)

The headline "% of WAL eliminated" depends entirely on how many heartbeats a
job emits, which is job_duration / heartbeat_interval. A 40-second review
emits one heartbeat; a 15-minute review emits thirty. The script therefore
reports the reduction as a function of job duration instead of a single
number, so the figure can be quoted with the assumption it rests on.

Caveats worth stating out loud:
  - Run against an otherwise idle database; autovacuum and checkpoints
    also advance the WAL LSN.
  - full_page_writes inflates the first write to each page after a
    checkpoint, so each phase is preceded by its own CHECKPOINT.

Usage:
    docker compose stop worker
    python scripts/benchmark_wal.py --ops 2000
"""

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from app.services.db.core import db_core  # noqa: E402

SETUP = """
DROP TABLE IF EXISTS wal_hb_logged;
DROP TABLE IF EXISTS wal_hb_unlogged;
CREATE TABLE wal_hb_logged (
    job_id       UUID PRIMARY KEY,
    worker_id    TEXT NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNLOGGED TABLE wal_hb_unlogged (
    job_id       UUID PRIMARY KEY,
    worker_id    TEXT NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DROP TABLE IF EXISTS wal_jobs_probe;
CREATE TABLE wal_jobs_probe (LIKE jobs INCLUDING ALL);
"""


async def _exec(sql: str, params: tuple = ()) -> None:
    pool = db_core.get_pool()
    async with pool.connection() as conn, conn, conn.cursor() as cur:
        await cur.execute(sql, params)


async def _scalar(sql: str) -> str:
    pool = db_core.get_pool()
    async with pool.connection() as conn, conn, conn.cursor() as cur:
        await cur.execute(sql)
        row = await cur.fetchone()
        return str(row[0]) if row else "0/0"


async def _wal_bytes_raw(workload) -> tuple[int, float]:
    """Run a workload; return (WAL bytes advanced, seconds elapsed)."""
    await _exec("CHECKPOINT")
    before = await _scalar("SELECT pg_current_wal_lsn()")
    start = time.monotonic()
    await workload()
    elapsed = time.monotonic() - start
    after = await _scalar("SELECT pg_current_wal_lsn()")
    pool = db_core.get_pool()
    async with pool.connection() as conn, conn, conn.cursor() as cur:
        await cur.execute("SELECT pg_wal_lsn_diff(%s::pg_lsn, %s::pg_lsn)", (after, before))
        row = await cur.fetchone()
        return (int(row[0]) if row else 0), elapsed


async def _idle_wal_rate(seconds: float) -> float:
    """Bytes/sec of WAL the server produces with no workload from us.

    Checkpoints, autovacuum and background writers advance the LSN on their
    own. Without subtracting this floor an UNLOGGED table appears to cost
    WAL that it does not actually generate.
    """
    total, elapsed = await _wal_bytes_raw(lambda: asyncio.sleep(seconds))
    return total / elapsed if elapsed else 0.0


async def _heartbeat_workload(table: str, job_ids: list[uuid.UUID], rounds: int) -> None:
    """Mirrors claim_job's upsert plus update_heartbeat's periodic touch."""
    pool = db_core.get_pool()
    async with pool.connection() as conn, conn, conn.cursor() as cur:
        for _ in range(rounds):
            for jid in job_ids:
                await cur.execute(
                    f"INSERT INTO {table} (job_id, worker_id, heartbeat_at) "  # noqa: S608
                    "VALUES (%s, %s, NOW()) "
                    "ON CONFLICT (job_id) DO UPDATE SET heartbeat_at = NOW()",
                    (jid, "worker-bench"),
                )


async def _job_lifecycle_workload(n: int) -> None:
    """Enqueue + claim + finalize, the durable writes a job must make."""
    pool = db_core.get_pool()
    async with pool.connection() as conn, conn, conn.cursor() as cur:
        for i in range(n):
            sha = f"wal_{uuid.uuid4().hex}"
            await cur.execute(
                "INSERT INTO wal_jobs_probe (commit_sha, pr_number, repo_full_name, payload, "
                "status, fence_token) VALUES (%s, %s, %s, %s, 'pending', 0)",
                (sha, i, "wal/repo", "{}"),
            )
            await cur.execute(
                "UPDATE wal_jobs_probe SET status = 'processing', started_at = NOW(), "
                "worker_id = %s, attempt_count = attempt_count + 1 WHERE commit_sha = %s",
                ("worker-bench", sha),
            )
            await cur.execute(
                "UPDATE wal_jobs_probe SET status = 'SUCCESS', completed_at = NOW(), "
                "result = %s WHERE commit_sha = %s",
                ('{"ok": true}', sha),
            )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ops", type=int, default=2000, help="heartbeat writes per phase")
    parser.add_argument("--jobs", type=int, default=500, help="job lifecycles to measure")
    parser.add_argument("--heartbeat-interval", type=int, default=30)
    parser.add_argument(
        "--baseline-seconds",
        type=float,
        default=5.0,
        help="Idle sampling window used to estimate and subtract the server's background WAL rate.",
    )
    args = parser.parse_args()

    await db_core.connect()
    await _exec(SETUP)

    job_ids = [uuid.uuid4() for _ in range(100)]
    rounds = max(args.ops // len(job_ids), 1)
    total_hb = rounds * len(job_ids)

    idle_rate = await _idle_wal_rate(args.baseline_seconds)

    def net(raw: int, elapsed: float) -> float:
        return max(raw - idle_rate * elapsed, 0.0)

    raw_logged, t_logged = await _wal_bytes_raw(
        lambda: _heartbeat_workload("wal_hb_logged", job_ids, rounds)
    )
    raw_unlogged, t_unlogged = await _wal_bytes_raw(
        lambda: _heartbeat_workload("wal_hb_unlogged", job_ids, rounds)
    )
    raw_lifecycle, t_lifecycle = await _wal_bytes_raw(lambda: _job_lifecycle_workload(args.jobs))

    logged = net(raw_logged, t_logged)
    unlogged = net(raw_unlogged, t_unlogged)
    lifecycle = net(raw_lifecycle, t_lifecycle)

    hb_logged_per_op = logged / total_hb
    hb_unlogged_per_op = unlogged / total_hb
    job_per_op = lifecycle / args.jobs

    print("\n" + "=" * 70)
    print("WAL VOLUME: UNLOGGED vs LOGGED HEARTBEATS")
    print("=" * 70)
    print(f"Heartbeat writes measured : {total_hb}")
    print(f"Job lifecycles measured   : {args.jobs}")
    print(f"Idle WAL floor subtracted : {idle_rate:,.0f} bytes/sec")
    print("-" * 70)
    print(f"LOGGED heartbeat table    : {logged:>12,.0f} bytes  ({hb_logged_per_op:>7.1f} B/write)")
    print(
        f"UNLOGGED heartbeat table  : {unlogged:>12,.0f} bytes  ({hb_unlogged_per_op:>7.1f} B/write)"
    )
    if hb_logged_per_op > 0:
        drop = (1 - hb_unlogged_per_op / hb_logged_per_op) * 100
        print(f"WAL removed per heartbeat : {drop:.1f}%")
    print(f"Job lifecycle (durable)   : {lifecycle:>12,.0f} bytes  ({job_per_op:>7.1f} B/job)")
    print("-" * 70)
    print("SHARE OF TOTAL WAL ELIMINATED, BY JOB DURATION")
    print(f"(heartbeat interval = {args.heartbeat_interval}s)")
    print(f"  {'job duration':>14} {'heartbeats':>11} {'total WAL/job':>15} {'eliminated':>12}")
    for duration in (60, 300, 900, 1800):
        beats = max(duration // args.heartbeat_interval, 1)
        hb_bytes = beats * hb_logged_per_op
        total = hb_bytes + job_per_op
        share = (hb_bytes - beats * hb_unlogged_per_op) / total * 100 if total else 0.0
        print(f"  {str(duration) + 's':>14} {beats:>11} {total:>14,.0f}B {share:>11.1f}%")
    print("-" * 70)
    print("Quote the figure together with the job duration it assumes.")
    print("Note: UNLOGGED removes WAL, not MVCC bloat — dead tuples and")
    print("autovacuum pressure on the heartbeat table are unchanged.")
    print("=" * 70 + "\n")

    await _exec("DROP TABLE IF EXISTS wal_hb_logged")
    await _exec("DROP TABLE IF EXISTS wal_hb_unlogged")
    await _exec("DROP TABLE IF EXISTS wal_jobs_probe")
    await db_core.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
