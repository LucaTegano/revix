import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
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
        patch.object(github_service.client, "post", AsyncMock(return_value=MagicMock(json=lambda: {"token": "new_token", "expires_at": "2025-01-01T00:00:00Z"}))),
        patch("app.services.github.db_core.get_pool", return_value=mock_pool)
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
        with pytest.raises(Exception):
            mock_res.raise_for_status.side_effect = httpx.HTTPStatusError("Rate limit", request=MagicMock(), response=mock_res)
            await github_service.fetch_diff("repo", 1, "token")

