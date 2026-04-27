from fastapi import status
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ping() -> None:
    response = client.post(
        "/api/webhooks/github",
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=dummy"},
    )
    # Signature will fail but we want to see FastAPI responding
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
