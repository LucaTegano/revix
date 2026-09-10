# Resilience: Measured Results

Every number here is produced by a script in `scripts/` that can be re-run.
Where a figure depends on an assumption, the assumption is stated next to it.

## 1. SIGKILL fault injection

**Harness:** [`scripts/chaos_sigkill.py`](../scripts/chaos_sigkill.py)

```bash
docker compose stop worker
python scripts/chaos_sigkill.py --jobs 700 --workers 8 --cycles 500 \
    --job-seconds 2.0 --jitter 1.0 --cycle-delay 0.8
```

Real worker processes claim jobs through the production queue repository and
are destroyed mid-job. Two fault modes are injected:

| Mode | Signal | What it exercises |
| :--- | :--- | :--- |
| Crash | `SIGKILL` | Heartbeat goes stale; reconciler requeues the job. |
| Stall | `SIGSTOP` → `SIGCONT` | Worker survives past the staleness threshold, wakes holding a **stale fence token**, and must be rejected at commit. |

The stall mode exists because a `SIGKILL`ed process can never come back, so
`SIGKILL` alone never exercises the fence token at all. The token defends
against the *stalled* worker — GC pause, network partition, cgroup throttling —
not the dead one.

### Result (500 fault cycles, 700 jobs, 8 worker processes)

| Metric | Result |
| :--- | :--- |
| Jobs recovered | **700 / 700 (100%)** |
| Jobs lost | 0 |
| `SIGKILL`s landed on in-flight jobs | 395 |
| Stall/resume cycles | 105 |
| Stale-token commits rejected | **105** (one per stall — fencing held in every case) |
| Detection latency (fault → requeued) | p50 3.05s · p95 3.66s · max 5.72s |
| Re-claim latency (fault → picked up again) | p50 3.47s · p95 4.32s · max 6.82s |

The queue kept making forward progress *during* the fault injection rather
than only draining afterwards — completions climbed steadily while workers
were being killed roughly once per second.

### Detection latency is a budget, not a constant

Detection is bounded by `WORKER_HEARTBEAT_STALE_SECONDS + WORKER_RECONCILE_INTERVAL_SECONDS`.
The harness compresses that budget to 3s + 1s = 4s so 500 cycles finish in
minutes; measured p95 of 3.66s sits inside it. **With production defaults
(90s + 60s) the same budget is 150s.** Detection latency scales with the
configured window; it is not an inherent 4 seconds.

Re-claim latency is deliberately reported separately: it is detection *plus*
waiting for a free worker slot, so it exceeds the detection budget whenever
every worker is already busy. Conflating the two would flatter the number.

## 2. Fencing protects the database, not the outside world

The harness records a stand-in for the external side effect (`post_review()`
in production) *before* `finalize_job()`, matching production ordering in
[`app/worker.py`](../app/worker.py).

| Metric | Result |
| :--- | :--- |
| Total side effects emitted | 833 |
| Jobs whose side effect ran more than once | **112 / 700** |

This is the honest reading of the architecture: the fence token makes the
**commit** exactly-once, and 105 stale commits were correctly rejected. But a
worker that is interrupted after posting to GitHub and before committing will
have its work redone by the next owner, so the **external effect is
at-least-once**. Closing that gap needs idempotency at the boundary — an
`Idempotency-Key` on the review comment, or upserting against the stored
check-run ID — not a stronger lock.

## 3. WAL volume eliminated by UNLOGGED heartbeats

**Harness:** [`scripts/benchmark_wal.py`](../scripts/benchmark_wal.py)

```bash
docker compose stop worker
python scripts/benchmark_wal.py --ops 4000 --jobs 800
```

Measured with `pg_current_wal_lsn()` before and after each workload, with a
`CHECKPOINT` between phases and the server's idle WAL rate subtracted.

| Workload | WAL generated |
| :--- | :--- |
| Heartbeat upsert, LOGGED table | 294.4 B/write |
| Heartbeat upsert, UNLOGGED table | 4.9 B/write |
| **WAL removed per heartbeat write** | **98.3%** |
| Full job lifecycle (durable writes) | 1,469 B/job |

### The share of *total* WAL removed depends on job duration

A job emits `duration / heartbeat_interval` heartbeats, so the saving is
large only when jobs are long relative to the heartbeat interval:

| Job duration | Heartbeats | Total WAL/job | Share eliminated |
| ---: | ---: | ---: | ---: |
| 60s | 2 | 2,058 B | 28.1% |
| 300s | 10 | 4,414 B | 65.6% |
| 900s | 30 | 10,302 B | 84.3% |
| 1800s | 60 | 19,135 B | 90.8% |

At 30s heartbeat intervals, a "~90% reduction in total WAL" holds only for
half-hour jobs. For a typical LLM review of 30s–3min the honest figure is
**28–50% of total WAL**. The defensible headline is the per-write number:
**UNLOGGED removes 98% of heartbeat WAL volume.**

### What UNLOGGED does not do

It removes write-ahead logging, replication and crash-safety for that table.
It does **not** reduce MVCC bloat: dead tuples and autovacuum pressure on the
heartbeat table are unchanged. The design trade is that heartbeats are
ephemeral — if the database restarts they are lost, and the reconciliation
loop rebuilds the state from `jobs`.

## Scope and limitations

- The harness stubs LLM inference and GitHub calls; the worker body sleeps.
  It validates queue durability, **not** review quality.
- Everything runs against a single local Postgres with worker processes on
  one host. `SKIP LOCKED` is agnostic to which process holds the connection,
  but multi-node operation is not what these runs measured.
- A production worker sharing the database will steal harness jobs and
  corrupt the result. The harness detects foreign consumers and fails rather
  than reporting a wrong number — but stop the worker first.
