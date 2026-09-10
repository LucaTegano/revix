"""SIGKILL fault-injection harness for the Postgres-native queue.

Spawns real worker *processes*, kills them with SIGKILL while they hold an
in-flight job, and verifies that the reconciliation loop recovers every job.

What is under test (the real production code paths, unmodified):
  - queue_repo.claim_job          (SKIP LOCKED claim + heartbeat upsert)
  - queue_repo.update_heartbeat   (liveness signal)
  - queue_repo.finalize_job       (fence-token guarded completion)
  - queue_repo.reconcile_stale_jobs (atomic requeue / dead-letter)

What is stubbed: the LLM inference and the GitHub API calls. The worker body
sleeps for a configurable duration to represent mid-inference work. This
harness measures queue durability, not review quality.

Three things are measured:
  1. Recovery rate  - every enqueued job must reach SUCCESS exactly once.
  2. Fencing        - a resurrected worker holding a stale token must be
                      rejected by finalize_job.
  3. Duplicate side effects - the external effect (post_review in production,
                      an insert into chaos_side_effects here) happens BEFORE
                      finalize_job, so a job that is killed after its side
                      effect but before its commit will re-run it. This is
                      at-least-once at the boundary and the harness reports
                      it rather than hiding it.

IMPORTANT: stop any production worker first (`docker compose stop worker`).
A real worker sharing the database will claim harness jobs, fail to fetch the
synthetic repo from GitHub, and finalize them as FAILURE — silently corrupting
the result. The harness detects foreign consumers and refuses to pass, but it
cannot prevent them.

Usage:
    docker compose stop worker
    python scripts/chaos_sigkill.py --jobs 60 --workers 6 --cycles 500

    # fast smoke run
    python scripts/chaos_sigkill.py --jobs 10 --workers 3 --cycles 20
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))


def _bootstrap_env() -> None:
    """Compress the recovery window before app.config is imported.

    app.config builds a cached Settings singleton at import time, so the
    tuning knobs have to be in os.environ first. Child processes inherit
    these, which is what keeps parent and worker in agreement.
    """
    argv = sys.argv

    def flag(name: str, default: str) -> str:
        if name in argv:
            return argv[argv.index(name) + 1]
        return default

    os.environ.setdefault("WORKER_HEARTBEAT_INTERVAL_SECONDS", flag("--heartbeat-interval", "1"))
    os.environ.setdefault("WORKER_HEARTBEAT_STALE_SECONDS", flag("--stale-threshold", "3"))
    os.environ.setdefault("WORKER_RECONCILE_INTERVAL_SECONDS", flag("--reconcile-interval", "1"))
    os.environ.setdefault("WORKER_RETRY_BACKOFF_SECONDS", flag("--retry-backoff", "0"))
    # Keep per-process pools small: this harness runs many processes at once.
    os.environ.setdefault("DB_POOL_MIN_SIZE", "2")
    os.environ.setdefault("WORKER_CONCURRENCY", "1")


_bootstrap_env()

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import logging  # noqa: E402
import random  # noqa: E402
import signal  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from typing import Any  # noqa: E402

from psycopg.rows import dict_row  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.db.core import db_core  # noqa: E402
from app.services.db.queue import queue_repo  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("chaos")
logger.setLevel(logging.INFO)

SHA_PREFIX = "chaos_"
REPO = "chaos/repo"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chaos_side_effects (
    id          BIGSERIAL PRIMARY KEY,
    commit_sha  TEXT NOT NULL,
    worker_id   TEXT NOT NULL,
    fence_token INT  NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS chaos_claims (
    id          BIGSERIAL PRIMARY KEY,
    commit_sha  TEXT NOT NULL,
    worker_id   TEXT NOT NULL,
    fence_token INT  NOT NULL,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS chaos_fence_rejections (
    id          BIGSERIAL PRIMARY KEY,
    commit_sha  TEXT NOT NULL,
    worker_id   TEXT NOT NULL,
    fence_token INT  NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS chaos_recoveries (
    id          BIGSERIAL PRIMARY KEY,
    commit_sha  TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS chaos_kills (
    id          BIGSERIAL PRIMARY KEY,
    commit_sha  TEXT NOT NULL,
    worker_id   TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'sigkill',
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


# --------------------------------------------------------------------------
# Worker role: a real process that claims, works, and commits.
# --------------------------------------------------------------------------


async def _record(sql: str, params: tuple[Any, ...]) -> None:
    pool = db_core.get_pool()
    async with pool.connection() as conn, conn, conn.cursor() as cur:
        await cur.execute(sql, params)


async def _heartbeat(job_id: uuid.UUID, worker_id: str) -> None:
    while True:
        await asyncio.sleep(settings.WORKER_HEARTBEAT_INTERVAL_SECONDS)
        with contextlib.suppress(Exception):
            if not await queue_repo.update_heartbeat(job_id, worker_id):
                return


async def run_worker(worker_id: str, job_seconds: float, jitter: float, effect_gap: float) -> None:
    await db_core.connect()
    while True:
        try:
            job = await queue_repo.claim_job(worker_id)
        except Exception:
            await asyncio.sleep(0.2)
            continue

        if not job:
            await asyncio.sleep(0.1)
            continue

        job_id = uuid.UUID(str(job["id"]))
        sha = str(job["commit_sha"])
        fence = int(job["fence_token"])

        await _record(
            "INSERT INTO chaos_claims (commit_sha, worker_id, fence_token) VALUES (%s, %s, %s)",
            (sha, worker_id, fence),
        )

        hb = asyncio.create_task(_heartbeat(job_id, worker_id))
        try:
            # Stand-in for LLM inference. This is the window in which SIGKILL lands.
            await asyncio.sleep(job_seconds + random.uniform(0, jitter))

            # Mirrors production ordering: the external side effect (post_review)
            # is committed to the outside world BEFORE the job row is finalized.
            await _record(
                "INSERT INTO chaos_side_effects (commit_sha, worker_id, fence_token) "
                "VALUES (%s, %s, %s)",
                (sha, worker_id, fence),
            )

            # In production post_review() is a network round-trip to GitHub, so
            # there is a real window between the external effect and the commit.
            # Modelling it is what makes the at-least-once boundary observable.
            if effect_gap:
                await asyncio.sleep(effect_gap)

            try:
                await queue_repo.finalize_job(job_id, fence, "SUCCESS", {"ok": True})
            except RuntimeError:
                # Fence token was bumped by the reconciler: this worker is a zombie.
                await _record(
                    "INSERT INTO chaos_fence_rejections (commit_sha, worker_id, fence_token) "
                    "VALUES (%s, %s, %s)",
                    (sha, worker_id, fence),
                )
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb


# --------------------------------------------------------------------------
# Orchestrator role
# --------------------------------------------------------------------------


class Orchestrator:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.kills_attempted = 0
        self.kills_landed = 0
        self.freezes_landed = 0
        self.frozen: set[str] = set()
        self.freeze_tasks: set[asyncio.Task[None]] = set()
        self.running = True

    async def setup(self) -> None:
        pool = db_core.get_pool()
        async with pool.connection() as conn, conn, conn.cursor() as cur:
            # Drop rather than truncate so schema changes to the harness tables
            # always take effect on the next run.
            for t in (
                "chaos_side_effects",
                "chaos_claims",
                "chaos_fence_rejections",
                "chaos_recoveries",
                "chaos_kills",
            ):
                await cur.execute(f"DROP TABLE IF EXISTS {t}")
            await cur.execute(SCHEMA)
            await cur.execute("DELETE FROM jobs WHERE commit_sha LIKE %s", (SHA_PREFIX + "%",))
            await cur.execute(
                "DELETE FROM review_records WHERE commit_sha LIKE %s", (SHA_PREFIX + "%",)
            )

    async def enqueue(self) -> list[str]:
        run_id = int(time.time())
        shas = [f"{SHA_PREFIX}{run_id}_{i}" for i in range(self.args.jobs)]
        for i, sha in enumerate(shas):
            await queue_repo.enqueue_if_new(sha=sha, repo=REPO, pull_number=i, installation_id=1)
        # A job killed N times has attempt_count N. With hundreds of kill cycles
        # the production default of 3 would dead-letter jobs for reasons that have
        # nothing to do with durability, so raise the ceiling for the experiment.
        pool = db_core.get_pool()
        async with pool.connection() as conn, conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE jobs SET max_attempts = %s WHERE commit_sha LIKE %s",
                (self.args.max_attempts, SHA_PREFIX + "%"),
            )
        return shas

    async def spawn_worker(self) -> str:
        worker_id = f"chaos-{uuid.uuid4().hex[:8]}"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--role",
            "worker",
            "--worker-id",
            worker_id,
            "--job-seconds",
            str(self.args.job_seconds),
            "--jitter",
            str(self.args.jitter),
            "--effect-gap",
            str(self.args.effect_gap),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.procs[worker_id] = proc
        return worker_id

    async def reconciler_loop(self) -> None:
        """Stands in for the worker's background reconciliation task."""
        while self.running:
            with contextlib.suppress(Exception):
                rows = await queue_repo.reconcile_stale_jobs()
                requeued = [r["id"] for r in rows if r["status"] == "pending"]
                if requeued:
                    # Stamp the moment the queue noticed the failure, so detection
                    # latency can be reported separately from re-claim latency.
                    await _record(
                        "INSERT INTO chaos_recoveries (commit_sha) "
                        "SELECT commit_sha FROM jobs WHERE id = ANY(%s)",
                        (requeued,),
                    )
            await asyncio.sleep(settings.WORKER_RECONCILE_INTERVAL_SECONDS)

    async def _inflight(self) -> list[dict[str, Any]]:
        pool = db_core.get_pool()
        async with pool.connection() as conn, conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT commit_sha, worker_id FROM jobs "
                "WHERE status = 'processing' AND commit_sha LIKE %s AND worker_id IS NOT NULL",
                (SHA_PREFIX + "%",),
            )
            return list(await cur.fetchall())

    async def _freeze(self, worker_id: str, sha: str) -> None:
        """SIGSTOP a worker past the staleness threshold, then SIGCONT it.

        A SIGKILLed process can never come back, so SIGKILL alone never
        exercises the fence token. This models the failure the token actually
        defends against: a worker that stalls (GC pause, network partition,
        cgroup throttling) long enough for the reconciler to requeue its job,
        then wakes up believing it still owns it.
        """
        proc = self.procs.get(worker_id)
        if proc is None or proc.returncode is not None:
            return
        try:
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, signal.SIGSTOP)
            await _record(
                "INSERT INTO chaos_kills (commit_sha, worker_id, mode) VALUES (%s, %s, 'freeze')",
                (sha, worker_id),
            )
            thaw = (
                settings.WORKER_HEARTBEAT_STALE_SECONDS
                + settings.WORKER_RECONCILE_INTERVAL_SECONDS
                + 1.5
            )
            await asyncio.sleep(thaw)
        finally:
            self.frozen.discard(worker_id)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, signal.SIGCONT)

    async def _pick_victim(self) -> dict[str, Any] | None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            candidates = [
                r
                for r in await self._inflight()
                if r["worker_id"] in self.procs and r["worker_id"] not in self.frozen
            ]
            if candidates:
                return random.choice(candidates)
            await asyncio.sleep(0.05)
        return None

    async def kill_loop(self, cycles: int) -> None:
        for cycle in range(1, cycles + 1):
            # Prefer interrupting a worker that actually holds a job: that is the
            # interesting failure, not killing an idle poller.
            victim = await self._pick_victim()
            self.kills_attempted += 1

            if victim is None:
                if not self.procs:
                    continue
                worker_id = random.choice(list(self.procs))
                sha = ""
            else:
                worker_id = str(victim["worker_id"])
                sha = str(victim["commit_sha"])

            # A fraction of cycles freeze instead of kill, so both the recovery
            # path and the fencing path get exercised in the same run.
            if sha and random.random() < self.args.freeze_ratio:
                self.freezes_landed += 1
                self.frozen.add(worker_id)
                task = asyncio.create_task(self._freeze(worker_id, sha))
                self.freeze_tasks.add(task)
                task.add_done_callback(self.freeze_tasks.discard)
            else:
                if sha:
                    self.kills_landed += 1
                proc = self.procs.pop(worker_id, None)
                if proc is not None and proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(proc.pid, signal.SIGKILL)
                    with contextlib.suppress(Exception):
                        await proc.wait()
                if sha:
                    await _record(
                        "INSERT INTO chaos_kills (commit_sha, worker_id, mode) "
                        "VALUES (%s, %s, 'sigkill')",
                        (sha, worker_id),
                    )
                await self.spawn_worker()

            # Pacing matters: with no delay the fleet is killed faster than a
            # job can finish, so nothing ever completes during the run and the
            # experiment only proves recovery after the chaos stops rather than
            # forward progress under sustained faults.
            if self.args.cycle_delay:
                await asyncio.sleep(self.args.cycle_delay)

            if cycle % 25 == 0 or cycle == cycles:
                done = await self._count_status("SUCCESS")
                logger.info(
                    "  cycle %4d/%d  kills=%d  freezes=%d  completed=%d/%d",
                    cycle,
                    cycles,
                    self.kills_landed,
                    self.freezes_landed,
                    done,
                    self.args.jobs,
                )

        if self.freeze_tasks:
            await asyncio.gather(*list(self.freeze_tasks), return_exceptions=True)

    async def _count_status(self, status: str) -> int:
        pool = db_core.get_pool()
        async with pool.connection() as conn, conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM jobs WHERE status = %s AND commit_sha LIKE %s",
                (status, SHA_PREFIX + "%"),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._count_status("SUCCESS") >= self.args.jobs:
                return True
            await asyncio.sleep(0.5)
        return False

    async def teardown(self) -> None:
        self.running = False
        for task in list(self.freeze_tasks):
            task.cancel()
        for proc in self.procs.values():
            if proc.returncode is None:
                # Thaw first: a stopped process would otherwise linger unreaped.
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, signal.SIGCONT)
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await proc.wait()
        self.procs.clear()
        self.frozen.clear()

    async def report(self, drained: bool) -> int:
        pool = db_core.get_pool()
        async with pool.connection() as conn, conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, count(*) AS n FROM jobs WHERE commit_sha LIKE %s GROUP BY status",
                (SHA_PREFIX + "%",),
            )
            by_status = {r["status"]: r["n"] for r in await cur.fetchall()}

            await cur.execute(
                "SELECT count(*) AS n FROM ("
                "  SELECT commit_sha FROM chaos_side_effects GROUP BY commit_sha HAVING count(*) > 1"
                ") d"
            )
            row = await cur.fetchone()
            dup_shas = int(row["n"]) if row else 0

            await cur.execute("SELECT count(*) AS n FROM chaos_side_effects")
            row = await cur.fetchone()
            total_effects = int(row["n"]) if row else 0

            await cur.execute("SELECT count(*) AS n FROM chaos_fence_rejections")
            row = await cur.fetchone()
            fence_rejections = int(row["n"]) if row else 0

            await cur.execute(
                "SELECT count(*) AS n FROM review_records WHERE commit_sha LIKE %s",
                (SHA_PREFIX + "%",),
            )
            row = await cur.fetchone()
            records = int(row["n"]) if row else 0

            # MTTR: for each kill, how long until that job was claimed again.
            await cur.execute(
                """
                SELECT EXTRACT(EPOCH FROM (c.claimed_at - k.at)) AS secs
                FROM chaos_kills k
                JOIN LATERAL (
                    SELECT claimed_at FROM chaos_claims c2
                    WHERE c2.commit_sha = k.commit_sha AND c2.claimed_at > k.at
                    ORDER BY c2.claimed_at ASC LIMIT 1
                ) c ON TRUE
                """
            )
            reclaims = [float(r["secs"]) for r in await cur.fetchall()]

            # Detection: fault injected -> reconciler requeued the job.
            await cur.execute(
                """
                SELECT EXTRACT(EPOCH FROM (r.at - k.at)) AS secs
                FROM chaos_kills k
                JOIN LATERAL (
                    SELECT at FROM chaos_recoveries r2
                    WHERE r2.commit_sha = k.commit_sha AND r2.at > k.at
                    ORDER BY r2.at ASC LIMIT 1
                ) r ON TRUE
                """
            )
            detections = [float(r["secs"]) for r in await cur.fetchall()]

            # Any consumer this harness did not spawn invalidates the run.
            await cur.execute(
                """
                SELECT DISTINCT worker_id FROM (
                    SELECT worker_id FROM chaos_claims
                    UNION
                    SELECT worker_id FROM jobs
                        WHERE commit_sha LIKE %s AND worker_id IS NOT NULL
                ) w WHERE worker_id NOT LIKE 'chaos-%%'
                """,
                (SHA_PREFIX + "%",),
            )
            foreign = [str(r["worker_id"]) for r in await cur.fetchall()]

        success = by_status.get("SUCCESS", 0)
        lost = self.args.jobs - success
        reclaims.sort()
        detections.sort()

        def pct(xs: list[float], p: float) -> float:
            if not xs:
                return 0.0
            return xs[min(int(len(xs) * p), len(xs) - 1)]

        print("\n" + "=" * 66)
        print("SIGKILL FAULT INJECTION — POSTGRES-NATIVE QUEUE")
        print("=" * 66)
        print(f"Jobs enqueued            : {self.args.jobs}")
        print(f"Workers (processes)      : {self.args.workers}")
        print(f"Fault cycles attempted   : {self.kills_attempted}")
        print(f"SIGKILL on in-flight job : {self.kills_landed}")
        print(f"SIGSTOP/CONT freezes     : {self.freezes_landed}")
        print(f"Stale threshold          : {settings.WORKER_HEARTBEAT_STALE_SECONDS}s")
        print(f"Reconcile interval       : {settings.WORKER_RECONCILE_INTERVAL_SECONDS}s")
        print("-" * 66)
        print("DURABILITY")
        print(f"  Jobs SUCCESS           : {success}/{self.args.jobs}")
        print(f"  Jobs lost              : {lost}")
        print(f"  Job status breakdown   : {by_status}")
        print(f"  review_records rows    : {records} (unique per commit_sha)")
        print("-" * 66)
        print("FENCING")
        print(f"  Stale-token rejections : {fence_rejections}")
        print("    (zombie workers blocked from committing after reconciliation)")
        print("-" * 66)
        print("SIDE-EFFECT SEMANTICS")
        print(f"  Total side effects     : {total_effects}")
        print(f"  Jobs effected >1 time  : {dup_shas}")
        print("    (external effect precedes commit -> at-least-once at the boundary)")
        print("-" * 66)
        budget = (
            settings.WORKER_HEARTBEAT_STALE_SECONDS + settings.WORKER_RECONCILE_INTERVAL_SECONDS
        )
        print("DETECTION LATENCY (fault -> job requeued by reconciler)")
        if detections:
            print(f"  samples                : {len(detections)}")
            print(
                f"  p50 / p95 / max        : {pct(detections, 0.50):.2f}s / "
                f"{pct(detections, 0.95):.2f}s / {detections[-1]:.2f}s"
            )
            print(f"  theoretical worst case : {budget}s (stale threshold + reconcile interval)")
        else:
            print("  no samples")
        print("-" * 66)
        print("RE-CLAIM LATENCY (fault -> job picked up again)")
        if reclaims:
            print(f"  samples                : {len(reclaims)}")
            print(
                f"  p50 / p95 / max        : {pct(reclaims, 0.50):.2f}s / "
                f"{pct(reclaims, 0.95):.2f}s / {reclaims[-1]:.2f}s"
            )
            print("    (detection + waiting for a free worker slot; exceeds the")
            print("     detection budget when every worker is already busy)")
        else:
            print("  no samples")
        print("=" * 66)

        if foreign:
            print("-" * 66)
            print("⚠️  FOREIGN CONSUMERS DETECTED — RESULT INVALID")
            for w in foreign[:5]:
                print(f"     {w}")
            print("     A worker outside this harness claimed harness jobs.")
            print("     Run `docker compose stop worker` and re-run.")

        ok = drained and lost == 0 and records == self.args.jobs and not foreign
        if ok:
            print(
                f"✅ PASS — 100% of jobs recovered across {self.kills_landed} SIGKILLs "
                f"and {self.freezes_landed} freezes"
            )
        else:
            print(f"❌ FAIL — {lost} job(s) never reached SUCCESS (drained={drained})")
        print("=" * 66 + "\n")
        return 0 if ok else 1

    async def run(self) -> int:
        await db_core.connect()
        await self.setup()
        await self.enqueue()
        logger.info("Enqueued %d jobs", self.args.jobs)

        for _ in range(self.args.workers):
            await self.spawn_worker()
        logger.info("Spawned %d worker processes", self.args.workers)

        recon = asyncio.create_task(self.reconciler_loop())
        try:
            logger.info("Injecting %d SIGKILL cycles...", self.args.cycles)
            await self.kill_loop(self.args.cycles)
            logger.info("Kill phase done. Draining...")
            drained = await self.drain(self.args.drain_timeout)
            return await self.report(drained)
        finally:
            self.running = False
            recon.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recon
            await self.teardown()
            await db_core.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default="orchestrator", choices=["orchestrator", "worker"])
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--jobs", type=int, default=60)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--cycles", type=int, default=500)
    parser.add_argument("--job-seconds", type=float, default=1.5)
    parser.add_argument("--jitter", type=float, default=1.0)
    parser.add_argument(
        "--effect-gap",
        type=float,
        default=0.3,
        help="Delay between the external side effect and finalize_job, modelling "
        "the post_review() network round-trip in production.",
    )
    parser.add_argument(
        "--cycle-delay",
        type=float,
        default=1.0,
        help="Seconds between fault cycles. Sets the fault rate relative to job "
        "duration; at 0 the fleet never makes forward progress.",
    )
    parser.add_argument(
        "--freeze-ratio",
        type=float,
        default=0.2,
        help="Fraction of fault cycles that SIGSTOP/SIGCONT a worker instead of "
        "SIGKILLing it, which is what exercises the fence token.",
    )
    parser.add_argument("--max-attempts", type=int, default=10_000)
    parser.add_argument("--drain-timeout", type=float, default=180.0)
    # Consumed by _bootstrap_env before app.config import; declared so --help
    # documents them and argparse does not reject them.
    parser.add_argument("--heartbeat-interval", default="1")
    parser.add_argument("--stale-threshold", default="3")
    parser.add_argument("--reconcile-interval", default="1")
    parser.add_argument("--retry-backoff", default="0")
    args = parser.parse_args()

    if args.role == "worker":
        try:
            asyncio.run(run_worker(args.worker_id, args.job_seconds, args.jitter, args.effect_gap))
        except KeyboardInterrupt:
            pass
        return 0

    return asyncio.run(Orchestrator(args).run())


if __name__ == "__main__":
    raise SystemExit(main())
