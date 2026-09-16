# Revix

A distributed AI code-review service for GitHub pull requests. Postgres-native
job queue (no Redis, no Celery), model-agnostic inference via LiteLLM, and a
fault-tolerant worker pool whose recovery behaviour is measured rather than
asserted.

[![CI](https://github.com/LucaTegano/revix/actions/workflows/ci.yml/badge.svg)](https://github.com/LucaTegano/revix/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Measured:** 700/700 jobs recovered across 500 fault-injection cycles ·
105/105 stale-token commits rejected · 49.2% fewer prompt tokens than
whole-file review · ~3,000 claims/s queue drain. Methodology and limits: [`docs/RESILIENCE.md`](docs/RESILIENCE.md),
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

### How it Works: Instant Delegation

FastAPI intercepts GitHub webhooks, verifies HMAC signatures for security, and instantly delegates the work to the **Postgres-Native Queue**. This ensures the API response is returned to GitHub in milliseconds, preventing timeouts while the heavy lifting happens in the background.

### Why Postgres-Native (and not Celery)?

LLM inference is slow and resource-intensive. Synchronous processing would block GitHub's APIs and cause timeouts. While Celery is a common choice for background tasks, Revix intentionally rejects it to:

- **Eliminate Infrastructure Bloat**: No need for Redis or RabbitMQ. PostgreSQL handles both state and queueing.
- **Solve Atomic Transitions**: Job state and data updates happen in a single ACID transaction, eliminating the "two-phase commit" problem.
- **Maintain Lean Connections**: Unlike standard task runners that can explode connection counts, our worker uses a stable pool and `SKIP LOCKED` for efficient, high-concurrency polling without contention.

## 📊 Performance & Validation

Every figure below is produced by a script in `scripts/` that can be re-run, and
is quoted with the assumption it rests on.

| Metric | Result | How it was measured |
| :--- | :--- | :--- |
| **Job recovery under fault injection** | **700 / 700** | 500 fault cycles — 395 `SIGKILL`s plus 105 stall/resume cycles — against 8 worker processes ([`chaos_sigkill.py`](scripts/chaos_sigkill.py)) |
| **Stale-token commits rejected** | **105 / 105** | Every resurrected worker was blocked at commit by its fence token |
| **Fault detection latency** | **p95 3.66s** | Against a compressed 4s budget; production defaults give 150s |
| **Heartbeat WAL volume removed** | **98%** | UNLOGGED vs LOGGED heartbeat table, `pg_current_wal_lsn()` diff ([`benchmark_wal.py`](scripts/benchmark_wal.py)) |
| **Queue dwell p99** | **249ms** | Enqueue-to-claim under a 1,000-job burst, 100 worker loops, no inference ([`benchmark_queue.py`](scripts/benchmark_queue.py)) |
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
  Measured: 112 of 700 jobs. Closing it needs idempotency at the boundary — an
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

### Accuracy benchmarking (SWE-bench Lite)

Diagnostic accuracy is validated against **SWE-bench Lite**, 300 real-world
Python PRs from Django, scikit-learn and Flask. The harness
([`benchmark_swe_bench.py`](scripts/benchmark_swe_bench.py)) ingests real issue
statements and fix patches and uses an LLM-as-judge to score whether the agents
aligned the implementation with the stated problem.

**Status: harness implemented and verified end-to-end; a full N=300 run needs
API quota this project does not have.** No accuracy figure is claimed until it
does.

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
├── .github/                  # 🐙 GitHub Actions CI/CD workflows
├── app/                      # 💻 Application source
│   ├── main.py               # Webhook ingestion with advisory locking
│   ├── worker.py             # Resilient background worker with heartbeat
│   └── services/             # Core services (ai.py, db.py)
├── docs/                     # 📚 Architecture, measured results, demo output
├── migrations/               # 🗄️ Alembic database migrations
├── scripts/                  # 📜 Utility and setup scripts
├── tests/                    # 🧪 Pytest test suite
├── .env.example              # Example environment variables
├── docker-compose.yml        # 🐳 Docker services configuration
├── Makefile                  # Build automation and development commands
└── README.md                 # 📖 Project documentation
```

📚 `docs/` - Documentation & Architecture
💻 `app/` - Application Source Code
🗄️ `migrations/` - Database Schema
🧪 `tests/` - Testing
📜 `scripts/` - Utility Scripts
