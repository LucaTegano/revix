import hashlib
import hmac

import pytest
from fastapi import HTTPException

from app.config import settings
from app.main import verify_signature


def test_verify_signature_valid() -> None:
    body = b'{"action": "opened"}'
    signature = (
        "sha256="
        + hmac.new(settings.GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    )

    # Should not raise
    verify_signature(body, signature)


def test_verify_signature_invalid() -> None:
    body = b'{"action": "opened"}'
    signature = "sha256=wrong"

    with pytest.raises(HTTPException) as excinfo:
        verify_signature(body, signature)
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "Invalid signature"


def test_verify_signature_missing() -> None:
    with pytest.raises(HTTPException) as excinfo:
        verify_signature(b"{}", "")
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "Missing signature"


def test_webhook_ping_without_valid_signature() -> None:
    from fastapi import status
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/webhooks/github",
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=dummy"},
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
