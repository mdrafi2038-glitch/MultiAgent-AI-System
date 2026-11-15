import pytest
from fastapi import HTTPException
import app.main as main


def test_password_auth_accepts_correct_password(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "test-secret")
    assert main.require_auth("test-secret") is True


def test_password_auth_rejects_wrong_password(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "test-secret")
    with pytest.raises(HTTPException) as error:
        main.require_auth("wrong")
    assert error.value.status_code == 401


def test_password_auth_fails_closed_when_not_configured(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "")
    with pytest.raises(HTTPException) as error:
        main.require_auth("anything")
    assert error.value.status_code == 503
