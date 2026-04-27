import hashlib
import json
import logging
import uuid
from typing import Any, cast

from psycopg.rows import dict_row

from app.config import settings
from app.services.db.core import db_core

logger = logging.getLogger(__name__)

class JobQueueRepository:
    def _generate_lock_id(self, sha: str) -> int:
        digest = hashlib.md5(sha.encode()).digest()
        val = int.from_bytes(digest[:8], byteorder="big", signed=True)
        return val

    async def enqueue_if_new(
        self,
        sha: str,
        repo: str,
        pull_number: int,
        installation_id: int,
        trace_context: dict[str, Any] | None = None
    ) -> bool:
        lock_id = self._generate_lock_id(sha)
        pool = db_core.get_pool()

        async with pool.connection() as conn:
            # psycopg v3: context manager on connection starts a transaction
            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
                    await cur.execute(
                        "SELECT 1 FROM review_records WHERE commit_sha = %s", (sha,)
                    )
                    exists = await cur.fetchone()
                    if exists:
                        return False

                    payload = {
                        "installation_id": installation_id,
                    }
                    await cur.execute(
                        """
                        INSERT INTO jobs (
                            commit_sha, pr_number, repo_full_name, payload,
                            otel_context, status, fence_token
                        )
                        VALUES (%s, %s, %s, %s, %s, 'pending', 0)
                        ON CONFLICT (commit_sha) DO NOTHING
                        """,
                        (sha, pull_number, repo, json.dumps(payload), json.dumps(trace_context or {}))
                    )
                    return True

    async def claim_job(self, worker_id: str) -> dict[str, Any] | None:
        pool = db_core.get_pool()
        query = """
            UPDATE jobs
            SET status = 'processing',
                started_at = NOW(),
                worker_id = %s,
                attempt_count = attempt_count + 1
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'pending'
                AND scheduled_at <= NOW()
                AND attempt_count < max_attempts
                ORDER BY priority DESC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            ) RETURNING *;
        """
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(query, (worker_id,))
                    row = await cur.fetchone()
                    if row:
                        await cur.execute(
                            """
                            INSERT INTO worker_heartbeats (job_id, worker_id, heartbeat_at)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (job_id) DO UPDATE SET heartbeat_at = NOW(), worker_id = %s
                            """,
                            (row['id'], worker_id, worker_id)
                        )
                        return cast(dict[str, Any], row)
                    return None

    async def update_heartbeat(self, job_id: uuid.UUID, worker_id: str) -> bool:
        pool = db_core.get_pool()
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT status FROM jobs WHERE id = %s AND worker_id = %s",
                        (job_id, worker_id)
                    )
                    row = await cur.fetchone()
                    if not row or row[0] != 'processing':
                        return False

                    await cur.execute(
                        """
                        UPDATE worker_heartbeats
                        SET heartbeat_at = NOW()
                        WHERE job_id = %s AND worker_id = %s
                        """,
                        (job_id, worker_id)
                    )
                    return True

    async def set_check_run_id(self, job_id: uuid.UUID, check_run_id: int) -> None:
        """Stores the GitHub Check Run ID for a job."""
        pool = db_core.get_pool()
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE jobs SET github_check_run_id = %s WHERE id = %s",
                        (check_run_id, job_id)
                    )

    async def get_latest_check_run_id(self, repo: str, pr_number: int) -> int | None:
        """Finds the most recent Check Run ID for a PR."""
        pool = db_core.get_pool()
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT github_check_run_id FROM jobs
                        WHERE repo_full_name = %s AND pr_number = %s
                        AND github_check_run_id IS NOT NULL
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (repo, pr_number)
                    )
                    row = await cur.fetchone()
                    return row[0] if row else None

    async def finalize_job(
        self,
        job_id: uuid.UUID,
        fence_token: int,
        status: str,
        ai_feedback: dict[str, Any] | None = None
    ) -> None:
        pool = db_core.get_pool()
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        UPDATE jobs
                        SET status = %s, completed_at = NOW(), result = %s
                        WHERE id = %s AND fence_token = %s
                        RETURNING id, commit_sha, pr_number, repo_full_name
                        """,
                        (status, json.dumps(ai_feedback) if ai_feedback else None, job_id, fence_token)
                    )
                    job = await cur.fetchone()

                    if job is None:
                        msg = f"Job {job_id} was reclaimed; fence_token {fence_token} is stale"
                        raise RuntimeError(msg)

                    await cur.execute(
                        """
                        INSERT INTO review_records (
                            job_id, commit_sha, pr_number, repo_full_name,
                            feedback, model_map, model_reduce, chunk_count
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (commit_sha) DO UPDATE SET feedback = EXCLUDED.feedback
                        """,
                        (
                            job['id'], job['commit_sha'], job['pr_number'], job['repo_full_name'],
                            json.dumps(ai_feedback) if ai_feedback else '{}',
                            settings.AI_MODEL_MAP, settings.AI_MODEL_REDUCE, 1
                        )
                    )

                    await cur.execute("DELETE FROM worker_heartbeats WHERE job_id = %s", (job_id,))

    async def release_job(self, job_id: uuid.UUID, fence_token: int, delay_seconds: int = 0) -> None:
        pool = db_core.get_pool()
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE jobs
                        SET status = 'pending',
                            worker_id = NULL,
                            scheduled_at = NOW() + (%s * INTERVAL '1 second'),
                            fence_token = fence_token + 1
                        WHERE id = %s AND fence_token = %s AND status = 'processing'
                        """,
                        (delay_seconds, job_id, fence_token)
                    )
                    await cur.execute("DELETE FROM worker_heartbeats WHERE job_id = %s", (job_id,))

    async def reconcile_stale_jobs(self) -> None:
        pool = db_core.get_pool()
        async with pool.connection() as conn:
            async with conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    # ADDED FOR UPDATE SKIP LOCKED
                    query = """
                        UPDATE jobs j
                        SET status = CASE
                            WHEN j.attempt_count >= j.max_attempts THEN 'dead'
                            ELSE 'pending'
                        END,
                        fence_token = j.fence_token + 1,
                        scheduled_at = CASE
                            WHEN j.attempt_count >= j.max_attempts THEN j.scheduled_at
                            ELSE NOW() + (INTERVAL '2 minutes' * j.attempt_count)
                        END,
                        worker_id = NULL
                        WHERE j.id IN (
                            SELECT id FROM jobs j2
                            WHERE j2.status = 'processing'
                            AND (
                                NOT EXISTS (SELECT 1 FROM worker_heartbeats h WHERE h.job_id = j2.id)
                                OR EXISTS (
                                    SELECT 1 FROM worker_heartbeats h
                                    WHERE h.job_id = j2.id
                                    AND h.heartbeat_at < NOW() - INTERVAL '90 seconds'
                                )
                            )
                            FOR UPDATE SKIP LOCKED
                        )
                        RETURNING j.id, j.status, j.attempt_count, j.github_check_run_id, j.repo_full_name, j.payload;
                    """
                    await cur.execute(query)
                    rows = await cur.fetchall()
                    if rows:
                        recovered = [r for r in rows if r['status'] == 'pending']
                        dead = [r for r in rows if r['status'] == 'dead']
                        if recovered:
                            logger.warning("Recovered %d dead worker jobs", len(recovered))
                        if dead:
                            logger.error("Moved %d jobs to dead letter queue", len(dead))

                    await cur.execute("""
                        DELETE FROM worker_heartbeats h
                        WHERE NOT EXISTS (
                            SELECT 1 FROM jobs j WHERE j.id = h.job_id AND j.status = 'processing'
                        )
                    """)

                    await cur.execute("""
                        UPDATE jobs
                        SET status = 'dead',
                            error_message = 'Timed out: pending for over 1 hour'
                        WHERE status = 'pending'
                        AND created_at < NOW() - INTERVAL '1 hour'
                    """)

queue_repo = JobQueueRepository()
