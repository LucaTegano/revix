import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import uuid
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.trace import Status, StatusCode
from pythonjsonlogger import json as jsonlogger

from app.config import settings
from app.services.ai import AIService
from app.services.db.core import db_core
from app.services.db.graph import graph_repo
from app.services.db.queue import queue_repo
from app.services.github import GitHubService

db_service = queue_repo

log_handler = logging.StreamHandler(sys.stdout)
if not settings.DEBUG:
    log_handler.setFormatter(
        jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
logging.basicConfig(
    handlers=[log_handler], level=logging.INFO if not settings.DEBUG else logging.DEBUG
)
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ReviewWorker:
    def __init__(self, concurrency: int = settings.WORKER_CONCURRENCY) -> None:
        self.ai = AIService()
        self.running = True
        self.worker_id = f"worker-{os.uname().nodename}-{uuid.uuid4().hex[:6]}"
        self._tasks: set[asyncio.Task[Any]] = set()
        self._semaphore = asyncio.Semaphore(concurrency)
        self._active_jobs: dict[str, dict[str, Any]] = {}  # job_id -> metadata (fence_token, etc)

    async def run_forever(self) -> None:
        logger.info("🚀 %s started. Concurrency: %d", self.worker_id, self._semaphore._value)
        await db_core.connect()
        await db_core.wait_for_tables(["jobs", "review_records", "worker_heartbeats"])

        # 1. Signal Trap
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        recon_task = asyncio.create_task(self.reconciliation_loop())
        recon_task.set_name("reconciliation")
        self._tasks.add(recon_task)

        # 2. Main Loop
        while self.running:
            try:
                await self._semaphore.acquire()
                if not self.running:
                    self._semaphore.release()
                    break

                job = await queue_repo.claim_job(self.worker_id)
                if job:
                    job_id_str = str(job["id"])
                    self._active_jobs[job_id_str] = {
                        "fence_token": job["fence_token"],
                        "commit_sha": job["commit_sha"],
                    }
                    task = asyncio.create_task(self._safe_process_job(job))
                    self._tasks.add(task)
                    task.add_done_callback(lambda t: self._tasks.discard(t))
                else:
                    self._semaphore.release()
                    await asyncio.sleep(5)
            except Exception:
                logger.exception("Worker loop error")
                if self.running:
                    self._semaphore.release()
                await asyncio.sleep(5)

        await self.shutdown()

    async def shutdown(self) -> None:
        """Exact sequence for graceful shutdown."""
        logger.info("🛑 Initiating graceful shutdown sequence...")

        if self._active_jobs:
            logger.info("⏳ Waiting %ds for active jobs...", settings.WORKER_SHUTDOWN_TIMEOUT)
            processing_tasks = [t for t in self._tasks if t.get_name() != "reconciliation"]
            if processing_tasks:
                _, pending = await asyncio.wait(
                    processing_tasks, timeout=float(settings.WORKER_SHUTDOWN_TIMEOUT)
                )
                if pending:
                    for task in pending:
                        task.cancel()

            for job_id_str, meta in list(self._active_jobs.items()):
                try:
                    await queue_repo.release_job(uuid.UUID(job_id_str), meta["fence_token"])
                    logger.info("✅ Released job %s", job_id_str)
                except Exception:
                    logger.error("❌ Failed to release job %s", job_id_str)

        if self._tasks:
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

        await db_core.disconnect()
        logger.info("👋 Shutdown complete.")

    def stop(self) -> None:
        self.running = False

    async def _safe_process_job(self, job: dict[str, Any]) -> None:
        job_id_str = str(job["id"])
        try:
            await self.process_job(job)
        finally:
            self._active_jobs.pop(job_id_str, None)
            self._semaphore.release()

    async def _run_heartbeat(self, job_id: uuid.UUID, sha: str) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                alive = await queue_repo.update_heartbeat(job_id, self.worker_id)
                if not alive:
                    return
            except Exception:
                logger.error("Heartbeat error for %s", sha)

    def _parse_job_payload(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            msg = "Job payload must be a JSON object"
            raise ValueError(msg)
        if "installation_id" not in payload:
            msg = "Job payload is missing installation_id"
            raise ValueError(msg)
        return payload

    def _parse_trace_context(self, trace_context: Any) -> dict[str, Any]:
        if isinstance(trace_context, str):
            trace_context = json.loads(trace_context)
        return trace_context if isinstance(trace_context, dict) else {}

    def _retry_delay_for(self, error: Exception, attempt_count: int) -> int:
        import time

        delay = max(60, 60 * max(attempt_count, 1))
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            retry_after = headers.get("retry-after")
            x_ratelimit_reset = headers.get("x-ratelimit-reset")
            if retry_after:
                with contextlib.suppress(ValueError):
                    return max(0, int(retry_after))
            if x_ratelimit_reset:
                with contextlib.suppress(ValueError):
                    return max(0, int(x_ratelimit_reset) - int(time.time()))
        return delay

    def _is_rate_limit(self, error: Exception) -> bool:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        return status_code == 429 or "429" in str(error) or "rate_limit" in str(error).lower()

    async def process_job(self, job: dict[str, Any]) -> None:
        job_id: uuid.UUID | None = None
        fence: int | None = None
        try:
            job_id = uuid.UUID(str(job["id"]))
            sha = str(job["commit_sha"])
            repo = str(job["repo_full_name"])
            pr_num = int(job["pr_number"])
            fence = int(job["fence_token"])
            payload = self._parse_job_payload(job.get("payload"))
            inst_id = int(payload["installation_id"])
            trace_ctx = self._parse_trace_context(job.get("otel_context"))
        except Exception as e:
            logger.exception("Invalid job payload")
            if job_id is not None and fence is not None:
                with contextlib.suppress(Exception):
                    await db_service.finalize_job(
                        job_id,
                        fence,
                        "FAILURE",
                        {"error": f"Invalid job payload: {e}"},
                    )
            return

        heartbeat = asyncio.create_task(self._run_heartbeat(job_id, sha))
        parent_context = propagate.extract(trace_ctx)

        with tracer.start_as_current_span("worker.process_job", context=parent_context) as span:
            span.set_attributes({"commit_sha": sha, "repo": repo, "worker_id": self.worker_id})
            github = GitHubService()
            check_run_id = None
            token: str | None = None
            try:
                token = await github.get_token(inst_id)

                # Create Check Run (The "Yellow Circle")
                try:
                    check_run_id = await github.create_check_run(repo, sha, token)
                    await db_service.set_check_run_id(job_id, check_run_id)
                except Exception as e:
                    logger.warning("Failed to create check run: %s", e)

                diff = await github.fetch_diff(repo, pr_num, token)
                pr_files = await github.fetch_pull_files(repo, pr_num, token)
                pr_details = await github.fetch_pull_request(repo, pr_num, token)

                # Index for call graph
                all_symbols, all_refs = [], []
                for f in pr_files:
                    if "patch" in f:
                        s, r = self.ai.indexer.index_file(f["filename"], f["patch"])
                        all_symbols.extend([vars(x) for x in s])
                        all_refs.extend([vars(x) for x in r])
                if all_symbols or all_refs:
                    await graph_repo.persist_repo_graph(repo, all_symbols, all_refs)

                review_result = await self.ai.analyze_diff(
                    diff=diff,
                    repo_full_name=repo,
                    pr_files=pr_files,
                    pr_details=pr_details,
                )

                # Aggregate comments into the main body instead of inline
                warnings_and_criticals = [
                    c for c in review_result.comments if c.severity in ("WARNING", "CRITICAL")
                ]

                # Conclusion based on score
                # Score >= 80 is success, < 80 is failure (blocking merge if required)
                conclusion = "success" if review_result.score >= 80 else "failure"

                body = (
                    f"### 🔍 Revix\n\n"
                    f"**Quality Score: {review_result.score}/100**\n\n"
                    f"{review_result.summary}\n\n"
                )

                if warnings_and_criticals:
                    body += "### ⚠️ Findings\n\n"
                    for c in warnings_and_criticals:
                        icon = "🚨" if c.severity == "CRITICAL" else "⚠️"
                        body += (
                            f"- {icon} **{c.severity}** in `{c.path}` (Line {c.line}): {c.body}\n"
                        )
                    body += "\n"

                formatted_comments = [
                    {
                        "path": c.path,
                        "line": c.line,
                        "side": c.side,
                        "body": f"[{c.severity}] {c.body}" + (f"\n\n**Suggested Fix:**\n```\n{c.suggested_fix}\n```" if c.suggested_fix else ""),
                    }
                    for c in review_result.comments
                ]

                await github.post_review(
                    repo=repo,
                    pull_number=pr_num,
                    commit_id=sha,
                    comments=formatted_comments,
                    token=token,
                    body=body,
                )

                if check_run_id:
                    await github.update_check_run(
                        repo=repo,
                        check_run_id=check_run_id,
                        token=token,
                        conclusion=conclusion,
                        output={
                            "title": f"Revix Review: {review_result.score}/100",
                            "summary": review_result.summary,
                            "text": f"Found {len(review_result.comments)} violations.",
                        },
                    )

                await db_service.finalize_job(job_id, fence, "SUCCESS", review_result.model_dump())
                logger.info("✅ Finalized %s with score %d", sha, review_result.score)
                span.set_status(Status(StatusCode.OK))

            except Exception as e:
                delay = self._retry_delay_for(e, int(job.get("attempt_count", 1)))
                if self._is_rate_limit(e):
                    logger.warning("Rate limited", extra={"commit_sha": sha, "delay": delay})
                    await db_service.release_job(job_id, fence, delay_seconds=delay)
                else:
                    logger.exception("Processing failed", extra={"commit_sha": sha})
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR))

                    if check_run_id and token:
                        try:
                            await github.update_check_run(
                                repo=repo,
                                check_run_id=check_run_id,
                                token=token,
                                conclusion="failure",
                                output={
                                    "title": "Review Failed",
                                    "summary": f"System Error: {str(e)}",
                                },
                            )
                        except Exception:
                            logger.error("Failed to update check run on error")

                    try:
                        await db_service.finalize_job(
                            job_id,
                            fence,
                            "FAILURE",
                            {"error": str(e)},
                        )
                    except Exception:
                        pass
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                await github.close()

    async def reconciliation_loop(self) -> None:
        while self.running:
            try:
                reconciled = await queue_repo.reconcile_stale_jobs()
                if reconciled:
                    github = GitHubService()
                    try:
                        for job in reconciled:
                            if job.get("github_check_run_id") and job["status"] in (
                                "dead",
                                "pending",
                            ):
                                # If it's dead or being retried, we should update the check run if it was in progress
                                try:
                                    payload = (
                                        json.loads(job["payload"])
                                        if isinstance(job["payload"], str)
                                        else job["payload"]
                                    )
                                    inst_id = payload.get("installation_id")
                                    if not inst_id:
                                        continue

                                    token = await github.get_token(inst_id)
                                    conclusion = (
                                        "failure" if job["status"] == "dead" else "action_required"
                                    )
                                    summary = "Job stalled or worker died."
                                    if job["status"] == "pending":
                                        summary += " Retrying analysis..."

                                    await github.update_check_run(
                                        repo=job["repo_full_name"],
                                        check_run_id=job["github_check_run_id"],
                                        token=token,
                                        conclusion=conclusion,
                                        output={"title": "Review Stalled", "summary": summary},
                                    )
                                except Exception as e:
                                    logger.warning(
                                        "Failed to cleanup Check Run %s: %s",
                                        job.get("github_check_run_id"),
                                        e,
                                    )
                    finally:
                        await github.close()
            except Exception:
                logger.exception("Reconciliation error")
            await asyncio.sleep(60)


if __name__ == "__main__":
    worker = ReviewWorker()
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        pass
