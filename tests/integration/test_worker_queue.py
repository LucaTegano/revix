import asyncio

import pytest

from app.services.db.core import db_core


@pytest.mark.asyncio
async def test_queue_enqueue_idempotency(queue_service):
    """Ensure enqueueing the same commit SHA twice only results in one row."""
    # First enqueue
    enqueued1 = await queue_service.enqueue_if_new("sha-idempotent", "repo/test", 1, 123, {})
    assert enqueued1 is True

    # Second enqueue
    enqueued2 = await queue_service.enqueue_if_new("sha-idempotent", "repo/test", 1, 123, {})
    assert enqueued2 is True

    # Verify count
    pool = db_core.get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM jobs WHERE commit_sha = %s", ("sha-idempotent",)
            )
            row = await cur.fetchone()
            count = row[0] if row else 0
            assert count == 1


@pytest.mark.asyncio
async def test_queue_concurrent_claim(queue_service):
    """Ensure two workers claiming simultaneously only give the job to one worker."""
    # Seed 1 job
    await queue_service.enqueue_if_new("sha-concurrent", "repo/test", 1, 123, {})

    # Claim simultaneously
    results = await asyncio.gather(
        queue_service.claim_job("worker-1"), queue_service.claim_job("worker-2")
    )

    # One should be a dict, the other should be None
    claimed_jobs = [r for r in results if r is not None]
    assert len(claimed_jobs) == 1
    assert claimed_jobs[0]["commit_sha"] == "sha-concurrent"
