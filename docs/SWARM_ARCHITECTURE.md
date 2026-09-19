# Agent Harness Architecture

Revix uses a deliberately small agent harness for pull-request review. The goal is to keep the useful agentic behavior without paying the complexity and latency cost of a coordinator plus several prompt-specialized agents.

## 1. AST-aware context selection

When a PR arrives, Revix retrieves the PR intent and changed files. Tree-sitter maps changed lines to logical syntax boundaries so the model reviews relevant functions, methods, and classes instead of blindly receiving entire files.

Files are ranked with deterministic risk signals and capped by the review chunk budget.

## 2. One Code Review Agent

Every selected chunk is reviewed by the same CodeReviewAgent. Its remit combines the former Review, Security, Performance, and Planning roles:

- correctness and edge cases
- security and data integrity
- performance problems that matter in production
- alignment with the PR title and description

This removes the LLM coordinator call and avoids running several copies of the same model with slightly different system prompts.

Chunks can still run concurrently, so the harness retains parallelism where it is useful.

## 3. Sandbox as a tool

Verification is a capability of the CodeReviewAgent rather than a separate agent. The model receives a `run_in_sandbox` tool and may generate a small standalone Python probe when execution can confirm or refute a concrete high-risk finding.

The probe runs in Docker with no network access and prefers the gVisor `runsc` runtime. stdout, stderr, and the exit code are returned to the model before it submits its final review.

## 4. Structured output

The agent submits a typed ReviewResult. Each finding includes the GitHub location, severity, explanation, and optional suggested fix. Pydantic validates the result before it reaches the GitHub integration.

## 5. Deterministic merge

Revix does not use another LLM to synthesize chunk summaries. Python merges results, removes duplicate findings, sorts them by severity and location, chooses the lowest chunk score as the global score, and creates a compact issue-count summary.

The resulting flow is:

```
PR
 -> Tree-sitter / risk ranking
 -> AST-scoped chunks
 -> CodeReviewAgent (parallel per chunk)
      -> optional run_in_sandbox
      -> submit_review
 -> deterministic dedupe / sort / summary
 -> GitHub
```

Compared with the previous swarm, this trades some specialist focus and intelligent routing for fewer model calls, lower latency, lower cost, and a substantially smaller failure surface.
