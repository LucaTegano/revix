import json
import logging
import time
from typing import Any, cast

import httpx
import jwt
from psycopg.rows import dict_row

from app.config import settings
from app.services.db.core import db_core

logger = logging.getLogger(__name__)


class GitHubService:
    BASE_URL = "https://api.github.com"
    MAX_PAYLOAD_SIZE = 60_000

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/vnd.github+json"}
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _generate_jwt(self) -> str:
        now = int(time.time())
        # GitHub allows maximum 10 minutes for JWT lifetime
        iat = now - 60
        exp = iat + (10 * 60)
        
        # Strip quotes and handle escaped newlines for production reliability
        raw_key = settings.github_app_private_key
        private_key = raw_key.replace("\\n", "\n").strip('"').strip("'")

        payload = {
            "iat": iat,
            "exp": exp,
            "iss": str(settings.GITHUB_APP_ID),
        }
        return jwt.encode(payload, private_key, algorithm="RS256")

    async def get_token(self, installation_id: int) -> str:
        """Fetches a fresh installation token from GitHub with Postgres caching."""
        pool = db_core.get_pool()

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("""
                    SELECT token FROM installation_tokens
                    WHERE installation_id = %s
                    AND expires_at > NOW() + INTERVAL '5 minutes'
                """, (installation_id,))
                cached = await cur.fetchone()

            if cached:
                return str(cached['token'])

            jwt_token = self._generate_jwt()
            resp = await self.client.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {jwt_token}"}
            )
            resp.raise_for_status()
            data = resp.json()

            token = str(data["token"])

            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO installation_tokens (installation_id, token, expires_at)
                        VALUES (%s, %s, NOW() + INTERVAL '55 minutes')
                        ON CONFLICT (installation_id) DO UPDATE
                        SET token = EXCLUDED.token,
                            expires_at = EXCLUDED.expires_at
                    """, (installation_id, token))

            return token

    async def fetch_diff(self, repo: str, pull_number: int, token: str) -> str:
        resp = await self.client.get(
            f"/repos/{repo}/pulls/{pull_number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.diff"
            }
        )
        resp.raise_for_status()
        return str(resp.text)

    async def fetch_pull_files(
        self, repo: str, pull_number: int, token: str
    ) -> list[dict[str, Any]]:
        """Fetches the list of files and their contents for a PR."""
        resp = await self.client.get(
            f"/repos/{repo}/pulls/{pull_number}/files",
            headers={"Authorization": f"Bearer {token}"}
        )
        resp.raise_for_status()
        files = resp.json()

        # For MVP we'll rely on the patch/diff provided in the files response
        return cast(list[dict[str, Any]], files)

    async def post_review(
        self,
        repo: str,
        pull_number: int,
        commit_id: str,
        comments: list[dict[str, Any]],
        token: str,
        body: str
    ) -> None:
        """Posts review comments to GitHub, chunking the payload if needed."""
        url = f"/repos/{repo}/pulls/{pull_number}/reviews"
        headers = {"Authorization": f"Bearer {token}"}

        signature = f"<!-- lucai-review: {commit_id} -->"
        if signature not in body:
            body += f"\n\n{signature}"
            
        try:
            resp = await self.client.get(url, headers=headers)
            resp.raise_for_status()
            for r in resp.json():
                if r.get("body") and signature in r["body"]:
                    logger.info("Review already exists for %s", commit_id)
                    return
        except Exception as e:
            logger.warning("Failed to check existing reviews: %s", e)

        # Map 'line' and 'side' for each comment
        # Note: GitHub Review API expects 'path', 'line', 'body', 'side' (optional)
        formatted_comments = []
        for c in comments:
            formatted_comments.append({
                "path": c["path"],
                "line": c["line"],
                "side": c.get("side", "RIGHT"),
                "body": c["body"]
            })

        # Post the main summary body as a single review
        await self._send_review_payload(
            url, headers, commit_id, body, []
        )

        # Post each comment as its own review to isolate validation failures
        for comment in formatted_comments:
            try:
                await self._send_review_payload(
                    url, headers, commit_id, "", [comment]
                )
            except Exception as e:
                logger.error("Failed to post comment %s: %s", comment, e)

    async def create_check_run(
        self, repo: str, head_sha: str, token: str
    ) -> int:
        """Creates a check run in progress."""
        url = f"/repos/{repo}/check-runs"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "name": "LucAI Quality Review",
            "head_sha": head_sha,
            "status": "in_progress",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        resp = await self.client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return int(resp.json()["id"])

    async def update_check_run(
        self,
        repo: str,
        check_run_id: int,
        token: str,
        conclusion: str,
        output: dict[str, Any]
    ) -> None:
        """Finalizes a check run."""
        url = f"/repos/{repo}/check-runs/{check_run_id}"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "output": output
        }
        resp = await self.client.patch(url, headers=headers, json=payload)
        resp.raise_for_status()

    async def _send_review_payload(
        self,
        url: str,
        headers: dict[str, str],
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "event": "COMMENT",
            "comments": comments,
        }
        if body:
            payload["body"] = body
        resp = await self.client.post(url, headers=headers, json=payload)
        if resp.status_code == 422:
            logger.error("❌ GitHub 422 Error: %s", resp.text)
            logger.error("Payload was: %s", json.dumps(payload, indent=2))
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())
