# Post-Mortem: Simulated Worker Crash Mid-Inference

## 1. Incident Summary
**Objective:** Validate the robustness of the Postgres-native `SKIP LOCKED` queue and background reconciliation system during an ungraceful worker termination while actively streaming data from the Anthropic API.
**Simulated Failure:** `SIGKILL` (kill -9) sent to a background worker process exactly 5 seconds into a 15-second Map phase AI inference step.
**Impact:** Zero dropped jobs. The job was stalled for exactly 5 minutes, caught by the reconciliation loop, and successfully retried by a healthy worker.

## 2. Timeline
*   `T+00:00`: Webhook ingested. `jobs` table row inserted with `status='pending'`.
*   `T+00:01`: Worker A (PID 1042) polls queue via `SELECT ... FOR UPDATE SKIP LOCKED`, sets `status='processing'`, and updates `locked_at=NOW()`.
*   `T+00:02`: Worker A begins Anthropic Map phase chunks.
*   `T+00:07`: **[FAULT INJECTED]** `kill -9 1042` executed. Worker A dies instantly.
    *   *Result:* The HTTP connection to Anthropic is severed. Crucially, because Worker A died outside of a database transaction (we release the DB connection during LLM wait times to save pool capacity), the `jobs` row remains indefinitely stuck in `status='processing'` with its old `locked_at` timestamp.
*   `T+00:07 - T+05:01`: The system is unaware the job has failed. OpenTelemetry traces show an unfinished span. Prometheus metrics show active processing count elevated.
*   `T+05:02`: Background `reconciliation_loop()` running on Worker B executes:
    ```sql
    UPDATE jobs
    SET status = 'pending', retry_count = retry_count + 1, locked_at = NULL
    WHERE status = 'processing' AND locked_at < NOW() - INTERVAL '5 minutes'
    RETURNING commit_sha;
    ```
*   `T+05:02`: Worker B identifies the stalled job, resets it to `pending`, and increments `retry_count` to `1`.
*   `T+05:03`: Worker B (or another healthy worker) immediately claims the job via its standard polling loop.
*   `T+05:18`: Map-Reduce completes successfully. Job marked as `SUCCESS`.

## 3. Metrics Behavior
During the incident, the following Prometheus/OTel metrics deviations were observed:
1.  **`ai_inference_duration_seconds`**: Initially showed a missing data point, but eventually recorded the successful retry (duration = 15s).
2.  **`queue_dwell_time_seconds`**: Spiked to `303s` (5 minutes + 3 seconds) for this specific trace, triggering a P99 queue delay alert.
3.  **`worker_reconciled_jobs_total`**: Incremented by 1, correctly tracking the system's self-healing action.

## 4. Root Cause & Architectural Validation
The failure mechanism (OOM kill, hardware failure, or pod eviction) is an expected reality in distributed systems. 

**Why Redis/SAQ would have failed:** If we had used Redis for job locks, a sudden pod death might leave the Redis key locked until its TTL expired. If the TTL is misconfigured (too short), another worker might pick it up while the first is still working. If too long, the delay is unacceptable.

**Why Postgres succeeded:** By explicitly modeling the `status` and `locked_at` state in a transactional database, and separating the "Queue Claim" transaction from the "AI Inference" network call, we achieved exactly-once delivery guarantees. The worker crash caused no database locks to leak, and the cron-like reconciliation query provided a deterministic, auditable recovery path.

## 5. Action Items
*   **Alerting:** Ensure the Datadog/Prometheus alert for `worker_reconciled_jobs_total > 0` is routed to a non-page engineering channel to track infrastructure flakiness.
*   **Trace Linking:** Update the `reconciliation_loop` to attach a trace event to the original `trace_id` noting the `CrashRecovery` so that the 5-minute dwell time is self-explanatory in tracing UIs.
