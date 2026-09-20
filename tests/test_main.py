from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture
def mock_queue_repo() -> Generator[MagicMock, None, None]:
    with patch("app.main.queue_repo") as mock:
        yield mock


def test_health_check() -> None:
    response = client.get("/health")
    assert response.status_code == status.HTTP_200_OK


def test_app_lifespan() -> None:
    with patch("app.main.db_core.connect", AsyncMock()):
        with patch("app.main.db_core.disconnect", AsyncMock()):
            with TestClient(app) as local_client:
                response = local_client.get("/health")
                assert response.status_code == 200


def test_webhook_ingestion(mock_queue_repo: MagicMock) -> None:
    # Setup mock
    mock_queue_repo.enqueue_if_new = AsyncMock(return_value=True)

    # Mock signature verification
    with patch("app.main.verify_signature", return_value=None):
        payload = {
            "action": "opened",
            "pull_request": {"number": 1, "head": {"sha": "sha123"}},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 123},
        }

        response = client.post(
            "/api/webhooks/github",
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=valid"},
            json=payload,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"msg": "accepted"}
        from unittest.mock import ANY

        mock_queue_repo.enqueue_if_new.assert_called_once_with(
            sha="sha123", repo="owner/repo", pull_number=1, installation_id=123, trace_context=ANY
        )


def test_webhook_already_exists(mock_queue_repo: MagicMock) -> None:
    # Setup mock
    mock_queue_repo.enqueue_if_new = AsyncMock(return_value=False)

    with patch("app.main.verify_signature", return_value=None):
        payload = {
            "action": "synchronize",
            "pull_request": {"number": 1, "head": {"sha": "sha123"}},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 123},
        }

        response = client.post(
            "/api/webhooks/github",
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=valid"},
            json=payload,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"msg": "already exists"}


def test_issue_comment_override_requires_authorization_and_pull_request(
    mock_queue_repo: MagicMock,
) -> None:
    with patch("app.main.verify_signature", return_value=None):
        payload = {
            "action": "created",
            "comment": {"body": "/revix-approve", "author_association": "NONE"},
            "issue": {"number": 1},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 123},
        }

        response = client.post(
            "/api/webhooks/github",
            headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": "sha256=valid"},
            json=payload,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_queue_repo.get_check_run_id.assert_not_called()


def test_authorized_issue_comment_cannot_override_normal_issue(mock_queue_repo: MagicMock) -> None:
    with patch("app.main.verify_signature", return_value=None):
        payload = {
            "action": "created",
            "comment": {"body": "/revix-approve", "author_association": "OWNER"},
            "issue": {"number": 1},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 123},
        }

        response = client.post(
            "/api/webhooks/github",
            headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": "sha256=valid"},
            json=payload,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_queue_repo.get_check_run_id.assert_not_called()


def test_issue_comment_override_binds_to_current_pr_head(mock_queue_repo: MagicMock) -> None:
    mock_queue_repo.get_check_run_id = AsyncMock(return_value=456)
    github = MagicMock()
    github.get_token = AsyncMock(return_value="token")
    github.fetch_pull_request = AsyncMock(return_value={"head": {"sha": "sha123"}})
    github.update_check_run = AsyncMock()
    github.close = AsyncMock()

    with (
        patch("app.main.verify_signature", return_value=None),
        patch("app.main.GitHubService", return_value=github),
    ):
        payload = {
            "action": "created",
            "comment": {"body": "/revix-approve", "author_association": "MEMBER"},
            "issue": {"number": 1, "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/1"}},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 123},
        }

        response = client.post(
            "/api/webhooks/github",
            headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": "sha256=valid"},
            json=payload,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_queue_repo.get_check_run_id.assert_awaited_once_with("owner/repo", 1, "sha123")
        github.update_check_run.assert_awaited_once()
