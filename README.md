# Revix

A distributed AI code-review service for GitHub pull requests. Postgres-native
job queue (no Redis, no Celery), model-agnostic inference via LiteLLM, and a
fault-tolerant worker pool whose recovery behaviour is measured rather than
asserted.

[![CI](https://github.com/LucaTegano/revix/actions/workflows/ci.yml/badge.svg)](https://github.com/LucaTegano/revix/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Measured:** 700/700 jobs recovered across 500 fault-injection cycles, twice ·
zero stale commits admitted · 49.2% fewer prompt tokens than whole-file review ·
~3,000 claims/s queue drain. Methodology and limits: [`docs/RESILIENCE.md`](docs/RESILIENCE.md),
[`docs/TOKENS.md`](docs/TOKENS.md).

## 🌟 What the System Does

A GitHub App webhook fires on a pull request; Revix reviews it and posts inline
comments plus a check run.

- **Postgres-native queue** — `SELECT ... FOR UPDATE SKIP LOCKED` for
  contention-free concurrent claims, with job state and payload updated in one
  ACID transaction.
- **Crash recovery with fencing** — workers heartbeat; a reconciliation loop
  requeues jobs whose owner went stale, and a monotonic fence token stops a
  resurrected worker from committing over its successor.
- **Diff-scoped AST chunking** — Tree-sitter parses the full file and the diff
  selects which syntax nodes to send, so agents see the changed functions rather
  than whole files. ([`docs/CHUNKING.md`](docs/CHUNKING.md))
- **Two-stage agent pipeline** — a routing coordinator decides which specialists
  (review, security, performance, planning, verification) each chunk needs, then
  a reducer synthesises their findings into one review.
- **Sandboxed execution** — the verification agent's generated test scripts run
  in a `--network=none` container with a read-only mount, under the gVisor
  (`runsc`) runtime where it is installed.
- **Idempotent ingestion** — `pg_advisory_xact_lock` keyed on the commit SHA
  collapses duplicate webhook deliveries.
- **Typed output** — Pydantic validation maps each finding to exact GitHub
  coordinates (path, line, side), dropping anything that will not apply cleanly.

## 🧠 How & Why: The Architecture

### Ingestion is decoupled from inference

FastAPI verifies the webhook's HMAC signature, takes a `pg_advisory_xact_lock`
on the commit SHA, inserts a job row, and returns. No inference happens on the
request path, so GitHub's delivery timeout is never at risk regardless of how
long a review takes. Workers claim from the same table.

### Why Postgres and not Celery

The job already needs a durable row — status, attempt count, fence token, the
posted check-run ID. Putting the queue in a second system means that row and the
queue entry can disagree, and every state transition becomes a distributed
commit across Postgres and the broker.

Keeping both in Postgres collapses that: claiming a job and updating its state
is one `UPDATE ... RETURNING` inside one transaction, so there is no window
where a job is claimed but not recorded. `SELECT ... FOR UPDATE SKIP LOCKED`
gives concurrent workers contention-free claims without an external broker, and
the operational cost of Redis or RabbitMQ disappears with it.

The trade is real and worth stating: this couples queue throughput to the
database, and a Postgres-backed queue will not match a dedicated broker at very
high message rates. For a workload where each job takes tens of seconds of LLM
inference, throughput is not the binding constraint — correctness across crashes
is, and that is what the single-transaction design buys.

## 📊 Performance & Validation

Every figure below is produced by a script in `scripts/` that can be re-run, and
is quoted with the assumption it rests on.

| Metric | Result | How it was measured |
| :--- | :--- | :--- |
| **Job recovery under fault injection** | **700 / 700**, twice | 500 fault cycles (`SIGKILL` + `SIGSTOP`/`SIGCONT` stalls) against 8 worker processes, run twice ([`chaos_sigkill.py`](scripts/chaos_sigkill.py)) |
| **Stale commits admitted** | **0** | 105 and 111 zombie commits rejected by fence token across the two runs; 700 unique `review_records` confirm no double-commit |
| **Fault detection latency** | **p95 3.66s** | Against a compressed 4s budget; production defaults give 150s |
| **Heartbeat WAL volume removed** | **98%** | UNLOGGED vs LOGGED heartbeat table, `pg_current_wal_lsn()` diff ([`benchmark_wal.py`](scripts/benchmark_wal.py)) |
| **Queue dwell p99** | **250ms** | Enqueue-to-claim under a 1,000-job burst, 100 worker loops, no inference ([`benchmark_queue.py`](scripts/benchmark_queue.py)) |
| **Prompt tokens vs full-file** | **−49.2%** | 262 files from `psf/requests` and `pallets/flask` ([`benchmark_tokens.py`](scripts/benchmark_tokens.py)) |

Three deep-dives cover the methodology, including where each guarantee stops:

- **[`docs/RESILIENCE.md`](docs/RESILIENCE.md)** — fault injection, fencing, and WAL.
  Includes the one that matters: `SIGKILL` alone never exercises a fence token,
  because a killed process cannot come back to present a stale one. Adding
  `SIGSTOP`/`SIGCONT` is what turned fencing from asserted into demonstrated.
- **[`docs/TOKENS.md`](docs/TOKENS.md)** — token measurement, validated against
  the provider's billed `usage.prompt_tokens`, with the saving broken down by
  diff size (55% on surgical diffs, ~4% on rewrites).
- **[`docs/CHUNKING.md`](docs/CHUNKING.md)** — the AST-chunking defect that made
  the token claim unreachable in production, and the fix.

### Known limits

Stated here rather than buried, because they change how the numbers read:

- **The external side effect is at-least-once.** The fence token makes the
  database commit exactly-once; `post_review()` reaches GitHub *before*
  `finalize_job`, so a worker interrupted between the two has its post replayed.
  Measured: 112 and 127 of 700 jobs across two runs. Closing it needs idempotency at the boundary — an
  `Idempotency-Key` on the comment, or an upsert against the check-run ID — not
  a stronger lock.
- **Token savings scale with diff size.** A PR touching under 5% of a file saves
  55%; one rewriting 40%+ of it saves ~4%.
- **The chaos harness stubs inference.** It validates queue durability, not
  review quality.
- **Queue dwell is bounded by the producer, not by `SKIP LOCKED`.** Enqueueing
  1,000 jobs takes 280–370ms on its own, so most of the recorded dwell elapsed
  before any worker could see the job. Drain throughput (~3,000 claims/s) is the
  more meaningful figure.
- **gVisor is best-effort, network isolation is not.** If `runsc` is not
  installed the sandbox logs a warning and retries under the default Docker
  runtime; `--network=none` and the read-only mount hold either way, but the
  syscall-interception boundary does not. Treat gVisor as defence in depth, not
  as the thing standing between you and untrusted code.

### Worked example on a real PR

[`docs/demo/`](docs/demo/) archives Revix reviewing a real C++ pull request,
where it flagged a data race (`splice()` mutating list pointers under a
`shared_lock`) and a use-after-free in an LRU eviction path, alongside a
head-to-head against the same model given one monolithic prompt. The diff under
review is embedded in [`scripts/test_fastcache_pr.py`](scripts/test_fastcache_pr.py),
so the findings are checkable from this repository alone.

### Review accuracy is not measured

There is no accuracy claim here, and that is deliberate.

[`scripts/defect_detection_probe.py`](scripts/defect_detection_probe.py) runs the
swarm and a single generic-prompt baseline over five hand-written diffs, each
seeding one known defect (authorization bypass, SQL injection, N+1 with a leaked
handle, None dereference, a fence-token race). It is useful as a regression
signal when prompts or routing change.

It is **not** a benchmark: n=5, and the fixtures were written by the same person
who wrote the system under test. An earlier revision of this README described
this as validation against SWE-bench Lite — "300 real-world Python PRs from
Django, scikit-learn and Flask". That was wrong; the script never ingested
SWE-bench, and the repository names in its fixtures are invented. The file was
named `benchmark_swe_bench.py`, which is what made the claim plausible, and has
been renamed.

A defensible accuracy figure needs a held-out public dataset the author did not
write, plus the API quota to run it. That work is open.

### Reproducing the numbers

Every figure in the table is a script. Stop the worker first — a live worker
sharing the database steals harness jobs (the chaos harness detects foreign
consumers and aborts rather than reporting a wrong number).

```bash
docker compose stop worker

# 700 jobs, 8 workers, 500 SIGKILL + stall/resume cycles
python scripts/chaos_sigkill.py --jobs 700 --workers 8 --cycles 500 \
    --job-seconds 2.0 --jitter 1.0 --cycle-delay 0.8

# WAL: UNLOGGED vs LOGGED heartbeat tables
python scripts/benchmark_wal.py --ops 4000 --jobs 800

# Enqueue-to-claim dwell under a 1,000-job burst
# (size the pool to the worker count, or you measure pool contention)
DB_POOL_MIN_SIZE=120 WORKER_CONCURRENCY=60 \
    python scripts/benchmark_queue.py --jobs 1000 --workers 100

# Prompt tokens vs a full-file baseline, over a real commit history
git clone --depth 300 https://github.com/psf/requests.git /tmp/requests
python scripts/benchmark_tokens.py --repo /tmp/requests --commits 250
```

Add `--live 12` to the token benchmark to re-validate LiteLLM's tokenizer
against the provider's billed `usage.prompt_tokens` (needs `AI_API_KEY`).

## 🛠 Tech Stack

- **API**: FastAPI (Python 3.12)
- **Database/Queue**: PostgreSQL 18
- **AI Orchestration**: LiteLLM
- **Tunnel**: ngrok (Dockerized)
- **Observability**: OpenTelemetry (Tracing & Metrics)

## ⚙️ Installation

### Prerequisites

- Docker and Docker Compose
- Make
- Python 3.12 (optional, for local development)
- GitHub App credentials

## 🚀 Usage

You can start the Revix system primarily through Docker Compose, using our automated Makefile for a smooth DevX.

### Option 1: Quick Start (Interactive Setup)

The fastest way to get Revix running is using the interactive setup wizard:

```bash
# Setup the environment and validate dependencies
make setup
```

This will:

- Check your local dependencies (Python, Docker).
- Create a `.env` file and guide you through configuring required keys.
- Validate your configuration immediately.

> [!TIP]
> For advanced users, we also provide `.env.minimal` for a "zero-noise" configuration.

### Option 2: Start the Stack Manually

Once configured, spin up the Postgres database and all 4 integrated containers (Webhook Ingestor, AI Agent Worker, DB, and ngrok):

```bash
# Start all services in detached mode
docker compose up -d --build
```

### Connect to GitHub

Run the following command to find your public tunneling URL, then paste it into your GitHub App settings:

```bash
docker compose logs ngrok
```

## 📁 Project Structure

```text
revix/
├── app/
│   ├── main.py               # Webhook ingestion, HMAC verify, advisory lock
│   ├── worker.py             # Claim loop, heartbeat, reconciliation
│   └── services/
│       ├── ai.py             # Chunking, agent swarm, synthesis
│       ├── github.py         # App auth, PR fetch, review posting
│       └── db/               # queue.py (SKIP LOCKED), core.py, graph.py
├── docs/
│   ├── RESILIENCE.md         # Fault injection, fencing, WAL, queue dwell
│   ├── TOKENS.md             # Prompt-token measurement and validation
│   ├── CHUNKING.md           # The AST-chunking defect and its fix
│   └── demo/                 # Archived review of a real PR
├── scripts/
│   ├── chaos_sigkill.py      # SIGKILL + SIGSTOP fault injection
│   ├── benchmark_queue.py    # Enqueue-to-claim dwell and drain rate
│   ├── benchmark_wal.py      # UNLOGGED vs LOGGED heartbeat WAL
│   ├── benchmark_tokens.py   # Diff-scoped vs full-file prompt tokens
│   └── defect_detection_probe.py  # n=5 seeded-defect smoke test
├── migrations/               # Alembic
├── tests/
└── docker-compose.yml
```

## 📄 License

MIT — see [LICENSE](LICENSE).
