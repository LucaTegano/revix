from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.github import GitHubService


@pytest.fixture
def github_service():
    return GitHubService()


def test_generate_jwt(github_service):
    with patch("app.services.github.settings") as mock_settings:
        mock_settings.GITHUB_APP_ID = 123
        mock_settings.github_app_private_key = "dummy_key"

        with patch("app.services.github.jwt.encode", return_value="jwt_token") as mock_encode:
            token = github_service._generate_jwt()
            assert token == "jwt_token"
            mock_encode.assert_called_once()


@pytest.mark.asyncio
async def test_get_token_cached(github_service):
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = AsyncMock()

    # Pool CM
    conn_cm = MagicMock()
    conn_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    conn_cm.__aexit__ = AsyncMock()
    mock_pool.connection.return_value = conn_cm

    # Conn CM
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock()

    # Cursor CM
    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=mock_cur)
    cur_cm.__aexit__ = AsyncMock()
    mock_conn.cursor.return_value = cur_cm

    mock_cur.fetchone.return_value = {"token": "cached_token"}

    with patch("app.services.github.db_core.get_pool", return_value=mock_pool):
        token = await github_service.get_token(123)
        assert token == "cached_token"


@pytest.mark.asyncio
async def test_get_token_refresh(github_service):
    # Test token refresh path (when cache is empty)
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = AsyncMock()

    mock_pool.connection.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)

    # First fetchone returns None (not in cache)
    mock_cur.fetchone.side_effect = [None, {"token": "new_token"}]

    # Mock JWT and installation token exchange
    with (
        patch.object(github_service, "_generate_jwt", return_value="jwt"),
        patch.object(
            github_service.client,
            "post",
            AsyncMock(
                return_value=MagicMock(
                    json=lambda: {"token": "new_token", "expires_at": "2025-01-01T00:00:00Z"}
                )
            ),
        ),
        patch("app.services.github.db_core.get_pool", return_value=mock_pool),
    ):
        token = await github_service.get_token(123)
        assert token == "new_token"
        assert mock_cur.execute.called


@pytest.mark.asyncio
async def test_fetch_diff(github_service):
    mock_response = MagicMock()
    mock_response.text = "diff_content"
    mock_response.raise_for_status = MagicMock()

    with patch.object(github_service.client, "get", AsyncMock(return_value=mock_response)):
        diff = await github_service.fetch_diff("owner/repo", 1, "token")
        assert diff == "diff_content"


@pytest.mark.asyncio
async def test_fetch_pull_files_list(github_service):
    mock_res = MagicMock()
    mock_res.json.return_value = [{"filename": "test.py", "patch": "@@ ..."}]
    mock_res.raise_for_status = MagicMock()

    with patch.object(github_service.client, "get", AsyncMock(return_value=mock_res)):
        files = await github_service.fetch_pull_files("repo", 1, "token")
        assert len(files) == 1
        assert files[0]["filename"] == "test.py"


@pytest.mark.asyncio
async def test_github_all_methods(github_service):
    mock_res = MagicMock()
    mock_res.json.return_value = {"id": 123, "state": "open"}
    mock_res.text = "diff"
    mock_res.raise_for_status = MagicMock()

    with patch.object(github_service.client, "get", AsyncMock(return_value=mock_res)):
        with patch.object(github_service.client, "post", AsyncMock(return_value=mock_res)):
            with patch.object(github_service.client, "patch", AsyncMock(return_value=mock_res)):
                pr = await github_service.fetch_pull_request("repo", 1, "token")
                assert pr["id"] == 123
                check_id = await github_service.create_check_run("repo", "sha", "token")
                assert check_id == 123
                await github_service.post_review("repo", 1, "sha", [], "token", "body")


@pytest.mark.asyncio
async def test_github_service_errors(github_service):
    mock_res = MagicMock()
    mock_res.headers = {"retry-after": "60"}
    mock_res.status_code = 429

    with patch.object(github_service.client, "get", AsyncMock(return_value=mock_res)):
        with pytest.raises(httpx.HTTPStatusError):
            mock_res.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Rate limit", request=MagicMock(), response=mock_res
            )
            await github_service.fetch_diff("repo", 1, "token")


# --- Source fetching for AST chunking -------------------------------------
#
# The chunker needs parseable file contents; the PR files endpoint only returns
# unified diffs. These cover the fetch path and, importantly, its degradations:
# a file without `content` must still be reviewable from its patch.


def _resp(status: int, content: bytes = b"") -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.content = content
    return r


@pytest.mark.asyncio
async def test_fetch_file_content_returns_source(github_service):
    github_service.client.get = AsyncMock(return_value=_resp(200, b"def f():\n    pass\n"))

    out = await github_service.fetch_file_content("o/r", "a.py", "sha1", "tok")

    assert out == "def f():\n    pass\n"
    _, kwargs = github_service.client.get.call_args
    assert kwargs["params"] == {"ref": "sha1"}
    assert kwargs["headers"]["Accept"] == "application/vnd.github.raw"


@pytest.mark.asyncio
async def test_fetch_file_content_rejects_oversized_file(github_service):
    big = b"x" * (GitHubService.MAX_SOURCE_FETCH_BYTES + 1)
    github_service.client.get = AsyncMock(return_value=_resp(200, big))

    assert await github_service.fetch_file_content("o/r", "big.py", "s", "t") is None


@pytest.mark.asyncio
async def test_fetch_file_content_rejects_binary(github_service):
    github_service.client.get = AsyncMock(return_value=_resp(200, b"\xff\xfe\x00\x01"))

    assert await github_service.fetch_file_content("o/r", "x.py", "s", "t") is None


@pytest.mark.asyncio
async def test_fetch_file_content_handles_missing_file(github_service):
    github_service.client.get = AsyncMock(return_value=_resp(404))

    assert await github_service.fetch_file_content("o/r", "gone.py", "s", "t") is None


@pytest.mark.asyncio
async def test_attach_source_skips_removed_and_unpatched(github_service):
    files = [
        {"filename": "kept.py", "patch": "@@ -1 +1 @@", "changes": 2},
        {"filename": "gone.py", "patch": "@@ -1 +0 @@", "changes": 1, "status": "removed"},
        {"filename": "nopatch.py", "changes": 3},
    ]
    github_service.fetch_file_content = AsyncMock(return_value="SRC")

    await github_service._attach_source("o/r", files, "sha", "tok")

    assert files[0]["content"] == "SRC"
    assert "content" not in files[1]
    assert "content" not in files[2]
    github_service.fetch_file_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_attach_source_survives_fetch_failure(github_service):
    """A failed fetch must degrade to patch-only review, not abort the job."""
    files = [{"filename": "a.py", "patch": "@@ -1 +1 @@", "changes": 2}]
    github_service.fetch_file_content = AsyncMock(side_effect=httpx.ConnectError("boom"))

    await github_service._attach_source("o/r", files, "sha", "tok")

    assert "content" not in files[0]


@pytest.mark.asyncio
async def test_attach_source_respects_fetch_budget(github_service):
    files = [
        {"filename": f"f{i}.py", "patch": "@@ -1 +1 @@", "changes": 2}
        for i in range(GitHubService.MAX_SOURCE_FETCHES + 5)
    ]
    github_service.fetch_file_content = AsyncMock(return_value="SRC")

    await github_service._attach_source("o/r", files, "sha", "tok")

    assert github_service.fetch_file_content.await_count == GitHubService.MAX_SOURCE_FETCHES


@pytest.mark.asyncio
async def test_fetch_pull_files_attaches_source_only_with_head_sha(github_service):
    payload = [{"filename": "a.py", "patch": "@@ -1 +1 @@", "changes": 2}]

    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    github_service.client.get = AsyncMock(return_value=resp)
    github_service._attach_source = AsyncMock()

    await github_service.fetch_pull_files("o/r", 1, "tok")
    github_service._attach_source.assert_not_awaited()

    await github_service.fetch_pull_files("o/r", 1, "tok", head_sha="abc")
    github_service._attach_source.assert_awaited_once()
