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

