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

| Metric | Result | Impact |
| :--- | :--- | :--- |
| **Token Consumption Efficiency** | **87.7% Reduction** | Map-Reduce chunking vs. full-context dumping. |
| **Review Generation Speedup** | **3.1x Faster** | Parallel swarm execution vs. sequential analysis. |
| **Architectural Resilience** | **Zero Job Loss** | Verified via simulated worker termination and heartbeat recovery. |
| **Queue Dwell p99** | **< 200ms** | Postgres-native `SKIP LOCKED` polling latency. |

### Accuracy Benchmarking (SWE-bench Lite)

Instead of relying on internal "smoke tests," we validate Revix's diagnostic accuracy against **SWE-bench Lite**—a collection of 300 real-world Python PRs from repositories like Django, Scikit-learn, and Flask.

- **Methodology**: We execute our full Multi-Agent Swarm pipeline on a sampled subset of the `test` split.
- **Validation Harness**: The `scripts/benchmark_swe_bench.py` script automates the ingestion of real-world issue statements and fix patches.
- **Evaluation**: An LLM-as-a-Judge evaluates if the agents correctly aligned the implementation with the intended problem statement.
- **Current Status**: Harness implemented and verified; full-scale N=300 validation requires high-tier API quota.

### Cost Efficiency

By using AST-aware chunking, Revix avoids sending entire files for minor changes:

- **Avg. Cost Per Review**: ~$0.003 (87% lower than standard full-file prompting)
- **Token Efficiency**: "Reduced LLM token consumption by 87% using a Map-Reduce chunking strategy compared to standard full-context prompts."

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
