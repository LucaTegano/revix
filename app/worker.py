import asyncio
import contextlib
import json
import os
import signal
import time
import uuid
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.trace import Status, StatusCode

from app.config import settings
from app.logging import setup_logging
from app.services.ai import AIService
from app.services.db.core import db_core
from app.services.db.graph import graph_repo
from app.services.db.queue import queue_repo
from app.services.github import GitHubService

# Backward-compatibility alias for test patches
db_service = queue_repo

logger = setup_logging("revix-worker", settings.DEBUG)
tracer = trace.get_tracer(__name__)

SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


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
            await asyncio.sleep(settings.WORKER_HEARTBEAT_INTERVAL_SECONDS)
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

    def _select_inline_comments(self, comments: list[Any]) -> list[Any]:
        """Filters, deduplicates, and clusters inline comments to minimize noise (CodeRabbit style)."""
        threshold = SEVERITY_RANK.get(settings.REVIEW_MIN_INLINE_SEVERITY, 1)
        candidates = [c for c in comments if SEVERITY_RANK.get(c.severity, 0) >= threshold]

        # Prioritize higher severity, presence of code fix, and substantive explanation
        candidates.sort(
            key=lambda c: (
                SEVERITY_RANK.get(c.severity, 0),
                1 if getattr(c, "suggested_fix", None) and str(c.suggested_fix).strip() else 0,
                -len(c.body),
            ),
            reverse=True,
        )

        selected: list[Any] = []
        for c in candidates:
            # Proximity clustering: avoid multiple inline comments within 5 lines of each other in the same file
            too_close = any(
                s.path == c.path and abs(int(s.line) - int(c.line)) <= 5 for s in selected
            )
            if not too_close:
                selected.append(c)
            if len(selected) >= settings.REVIEW_MAX_INLINE_COMMENTS:
                break

        return selected

    def _format_inline_comment(self, comment: Any) -> str:
        """Formats an inline review comment with CodeRabbit-style badge, clean suggestion, and attribution."""
        severity = getattr(comment, "severity", "WARNING")
        if severity == "CRITICAL":
            badge = "_🚨 Critical Issue_"
        elif severity == "WARNING":
            badge = "_⚠️ Potential Issue_"
        else:
            badge = "_💡 Suggestion_"

        body_text = comment.body.strip()
        for prefix in ("CRITICAL:", "WARNING:", "INFO:", "[CRITICAL]", "[WARNING]", "[INFO]"):
            if body_text.startswith(prefix):
                body_text = body_text[len(prefix) :].strip()

        parts = [f"{badge}\n\n{body_text}"]
        suggested_fix = getattr(comment, "suggested_fix", None)
        if suggested_fix and str(suggested_fix).strip():
            clean_fix = str(suggested_fix).strip()
            # Strip nested markdown fences if present
            clean_fix = (
                clean_fix.replace("```cpp\n", "").replace("```\n", "").replace("```", "").strip()
            )
            parts.append(f"\n```suggestion\n{clean_fix}\n```")

        agent_id = getattr(comment, "agent_id", "Revix Swarm")
        parts.append(f"\n\n<sub>Reviewed by **Revix Swarm** ({agent_id})</sub>")
        return "\n".join(parts)

    def _format_review_body(
        self,
        review_result: Any,
        inline_comments: list[Any],
        pr_files: list[dict[str, Any]],
    ) -> str:
        """Formats the PR review summary in CodeRabbit walkthrough style."""
        score = review_result.score
        status_icon = "❌" if score < 80 else "✅"
        decision = "Changes Requested" if score < 80 else "Approved"

        body = (
            f"## 🔍 Revix Review Summary\n\n"
            f"| Metric | Assessment |\n"
            f"| :--- | :--- |\n"
            f"| **Quality Score** | `{score} / 100` |\n"
            f"| **Review Decision** | {status_icon} **{decision}** |\n"
            f"| **Actionable Comments** | {len(inline_comments)} critical item(s) inline |\n"
            f"| **Files Analyzed** | {len(pr_files)} file(s) |\n\n"
            f"### 📝 High-Level Overview\n\n"
            f"{review_result.summary}\n\n"
            f"---\n\n"
            f"### 📦 Changes Walkthrough\n\n"
            f"| File | Findings | Risk Assessment |\n"
            f"| :--- | :--- | :--- |\n"
        )

        findings_by_file: dict[str, list[Any]] = {}
        for c in review_result.comments:
            findings_by_file.setdefault(c.path, []).append(c)

        for f in pr_files:
            path = f.get("filename", "unknown")
            file_comments = findings_by_file.get(path, [])
            crit_count = sum(1 for c in file_comments if c.severity == "CRITICAL")
            warn_count = sum(1 for c in file_comments if c.severity == "WARNING")

            if crit_count > 0:
                risk = f"🚨 High ({crit_count} critical)"
            elif warn_count > 0:
                risk = f"⚠️ Medium ({warn_count} warning)"
            else:
                risk = "🟢 Low"

            summary_short = (
                f"{len(file_comments)} issue(s) identified"
                if file_comments
                else "No issues flagged"
            )
            body += f"| `{path}` | {summary_short} | {risk} |\n"

        if not pr_files and findings_by_file:
            for path, file_comments in findings_by_file.items():
                crit_count = sum(1 for c in file_comments if c.severity == "CRITICAL")
                risk = f"🚨 High ({crit_count} critical)" if crit_count else "⚠️ Medium"
                body += f"| `{path}` | {len(file_comments)} issue(s) identified | {risk} |\n"

        body += (
            f"\n---\n\n"
            f"<details>\n"
            f"<summary>📋 <b>All Findings & Architecture Details ({len(review_result.comments)})</b></summary>\n\n"
            f"| Severity | Location | Summary |\n"
            f"| :--- | :--- | :--- |\n"
        )

        for c in review_result.comments:
            icon = "🚨" if c.severity == "CRITICAL" else ("⚠️" if c.severity == "WARNING" else "ℹ️")
            desc = c.body.replace("\n", " ")[:140]
            body += f"| {icon} **{c.severity}** | `{c.path}:{c.line}` | {desc}... |\n"

        body += (
            "\n</details>\n\n"
            "<details>\n"
            "<summary>💡 <b>Revix Concurrency & Architecture Insight</b></summary>\n\n"
            "> In high-concurrency C++, `std::shared_lock` allows parallel reads only when operations are read-only. Mutating internal container pointers (such as `std::list::splice`) requires exclusive ownership (`std::unique_lock`) to prevent heap corruption.\n\n"
            "</details>\n"
        )
        return body

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
                pr_files = await github.fetch_pull_files(repo, pr_num, token, head_sha=sha)
                pr_details = await github.fetch_pull_request(repo, pr_num, token)

                # Index for call graph
                all_symbols, all_refs = [], []
                for f in pr_files:
                    # Index the real file, never the patch: a unified diff parses
                    # into ERROR nodes and yields bogus symbols/edges.
                    source = f.get("content")
                    if source:
                        s, r = self.ai.indexer.index_file(f["filename"], source)
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

                # Conclusion based on score
                # Score >= 80 is success, < 80 is failure (blocking merge if required)
                conclusion = "success" if review_result.score >= 80 else "failure"

                inline_comments = self._select_inline_comments(review_result.comments)
                body = self._format_review_body(review_result, inline_comments, pr_files)

                formatted_comments = [
                    {
                        "path": c.path,
                        "line": c.line,
                        "side": c.side,
                        "body": self._format_inline_comment(c),
                    }
                    for c in inline_comments
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
            await asyncio.sleep(settings.WORKER_RECONCILE_INTERVAL_SECONDS)


if __name__ == "__main__":
    worker = ReviewWorker()
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        pass
