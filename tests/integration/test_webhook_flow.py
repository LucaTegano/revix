import hashlib
import hmac
import json

from psycopg.rows import dict_row

import pytest

from app.config import get_settings
from app.services.db.core import db_core


@pytest.mark.asyncio
async def test_github_webhook_e2e(app_client, queue_service):
    """Tests the E2E flow from receiving a webhook to inserting into the database."""
    settings = get_settings()
    
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 123,
            "head": {"sha": "abcdef123456"}
        },
        "repository": {
            "full_name": "test/repo"
        },
        "installation": {
            "id": 456
        }
    }
    
    body = json.dumps(payload).encode()
    signature = hmac.new(
        settings.GITHUB_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    
    response = await app_client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": f"sha256={signature}",
            "Content-Type": "application/json"
        }
    )
    
    assert response.status_code == 200
    assert response.json() == {"msg": "accepted"}
    
    # Verify the database has the job
    pool = db_core.get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM jobs WHERE commit_sha = %s", ("abcdef123456",))
            job = await cur.fetchone()
            assert job is not None
            assert job["repo_full_name"] == "test/repo"
            assert job["status"] == "pending"
