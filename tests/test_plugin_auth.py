import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException

from app.plugin_hub import require_auth


def _token(password: str, expiry: int) -> str:
    payload = f"dashboard-session:{expiry}".encode()
    signature = hmac.new(password.encode(), payload, hashlib.sha256).hexdigest()
    return f"{expiry}.{signature}"


def test_custom_plugin_auth_accepts_remembered_session(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "test-secret")
    token = _token("test-secret", int(time.time()) + 3600)
    assert require_auth(None, token) is True


def test_custom_plugin_auth_rejects_tampered_session(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "test-secret")
    token = _token("test-secret", int(time.time()) + 3600)
    with pytest.raises(HTTPException) as error:
        require_auth(None, token + "tampered")
    assert error.value.status_code == 401


def test_custom_plugin_auth_keeps_password_header_compatibility(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "test-secret")
    assert require_auth("test-secret", None) is True
