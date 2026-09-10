# Revix - Staff-Tier Distributed AI Code Reviewer

Revix is a high-performance, asynchronous code review system built for enterprise-scale repositories. It rejects infrastructure bloat (no Redis, no Celery) in favor of a **Postgres-Native** architecture and **Model-Agnostic** intelligence via LiteLLM.

This project was developed by Luca as an AI-powered code review agent that scales massively while keeping latency and costs extremely low. The implementation particularly focuses on demonstrating enterprise architecture principles using a resilient distributed queue pattern. Detailed reports on architectural decisions can be found in the `docs` directory.

## 🌟 What the System Does

Revix acts as an automated staff-level engineer that reviews Pull Requests seamlessly via GitHub Webhooks. The system supports:

- **Distributed Queuing**: Postgres-native task queue utilizing `SKIP LOCKED` for massive horizontal scaling without two-phase commit overhead.
- **Multi-Agent Swarm AI**: Orchestrates a sophisticated swarm of specialized agents (Review, Security, Performance, Planning, Verification) for zero-pollution context analysis.
- **gVisor Sandbox Execution**: Proactively tests code changes by executing agent-generated scripts in a hardened `runsc` (gVisor) sandbox.
- **Idempotent Webhooks**: Uses `pg_advisory_xact_lock` with MD5-hashed commit SHAs to prevent race conditions during ingestion.
- **Resilient Recovery**: Background reconciliation tasks that automatically detect and restart jobs from mid-inference worker crashes.
- **JSON Coercion**: Strict Pydantic validation of AI feedback mapping defects to exact GitHub PR coordinates (line, side, path).
- **Integrated Tunneling & DevX**: Includes an ngrok container that automatically starts the tunnel with the app, simplifying GitHub App webhook testing.

## 🧠 How & Why: The Architecture

### How it Works: Instant Delegation

FastAPI intercepts GitHub webhooks, verifies HMAC signatures for security, and instantly delegates the work to the **Postgres-Native Queue**. This ensures the API response is returned to GitHub in milliseconds, preventing timeouts while the heavy lifting happens in the background.

### Why Postgres-Native (and not Celery)?

LLM inference is slow and resource-intensive. Synchronous processing would block GitHub's APIs and cause timeouts. While Celery is a common choice for background tasks, Revix intentionally rejects it to:

- **Eliminate Infrastructure Bloat**: No need for Redis or RabbitMQ. PostgreSQL handles both state and queueing.
- **Solve Atomic Transitions**: Job state and data updates happen in a single ACID transaction, eliminating the "two-phase commit" problem.
- **Maintain Lean Connections**: Unlike standard task runners that can explode connection counts, our worker uses a stable pool and `SKIP LOCKED` for efficient, high-concurrency polling without contention.

## 📊 Performance & Validation

Revix is measured by system efficiency and detection capability, using industry-standard datasets to avoid "toy-project" metrics.

### System Metrics

Measured figures and the scripts that produce them are in
[`docs/RESILIENCE.md`](docs/RESILIENCE.md). Each is quoted with the assumption
it rests on.

| Metric | Result | How it was measured |
| :--- | :--- | :--- |
| **Job recovery under fault injection** | **700/700 (100%)** | 500 fault cycles — 395 `SIGKILL`s plus 105 stall/resume cycles — against 8 worker processes (`scripts/chaos_sigkill.py`). |
| **Stale-token commits rejected** | **105 / 105** | Every resurrected worker was blocked at commit by the fence token. |
| **Fault detection latency** | **p95 3.66s** | Against a compressed 4s budget (3s staleness + 1s reconcile). Production defaults give a 150s budget. |
| **WAL removed per heartbeat write** | **98.3%** | UNLOGGED vs LOGGED heartbeat table, `pg_current_wal_lsn()` diff with the idle WAL floor subtracted (`scripts/benchmark_wal.py`). |
| **Queue dwell p99** | **< 200ms** | Enqueue-to-claim under a 1,000-job burst with inference mocked (`scripts/benchmark_queue.py`). |

Two caveats stated up front, because they change how the numbers should be
read: the external side effect is **at-least-once** (112 of 700 jobs had their
side effect replayed — the fence token makes the *commit* exactly-once, not the
GitHub post), and the share of *total* WAL eliminated by UNLOGGED heartbeats
ranges from 28% to 91% depending on job duration.

Token-efficiency and pipeline-speedup figures previously published here were
derived from a model with hardcoded latency constants rather than from
measurement, and have been withdrawn pending real `prompt_tokens` data from
LiteLLM.

### Accuracy Benchmarking (SWE-bench Lite)

Instead of relying on internal "smoke tests," we validate Revix's diagnostic accuracy against **SWE-bench Lite**—a collection of 300 real-world Python PRs from repositories like Django, Scikit-learn, and Flask.

- **Methodology**: We execute our full Multi-Agent Swarm pipeline on a sampled subset of the `test` split.
- **Validation Harness**: The `scripts/benchmark_swe_bench.py` script automates the ingestion of real-world issue statements and fix patches.
- **Evaluation**: An LLM-as-a-Judge evaluates if the agents correctly aligned the implementation with the intended problem statement.
- **Current Status**: Harness implemented and verified; full-scale N=300 validation requires high-tier API quota.

### Cost Efficiency

Revix uses Tree-sitter chunking to scope each agent's context to the relevant
syntax blocks rather than dumping whole files, and routes a coordinator pass
before fanning out to sub-agents. The intended effect is fewer tokens per
review.

**This has not yet been measured.** The earlier "~87% reduction" came from
`scripts/benchmark_ai_pipeline.py`, which estimates tokens as `len(text) // 4`
and compares against a hypothetical baseline rather than a recorded one. A
defensible figure requires logging real `usage.prompt_tokens` from LiteLLM
across a set of PRs against a full-file control, which is tracked as open work.

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
├── app/                      # 💻 Source code (Microservices architecture)
│   ├── main.py               # Webhook ingestion with advisory locking
│   ├── worker.py             # Resilient background worker with heartbeat
│   └── services/             # Core services (ai.py, db.py)
├── docs/                     # 📚 Documentation & Architecture deep dives
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
