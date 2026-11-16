import pytest
from fastapi import HTTPException
import app.main as main


def test_password_auth_accepts_correct_password(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "test-secret")
    assert main.require_auth("test-secret") is True


def test_password_auth_rejects_wrong_password(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "test-secret")
    with pytest.raises(HTTPException) as error:
        main.require_auth("wrong", None)
    assert error.value.status_code == 401


def test_password_auth_fails_closed_when_not_configured(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "")
    with pytest.raises(HTTPException) as error:
        main.require_auth("anything", None)
    assert error.value.status_code == 503



def test_remembered_session_cookie_authenticates_without_password(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "test-secret")
    expiry = int(main.time.time()) + 3600
    token = main._session_token(expiry)
    assert main.require_auth(None, token) is True


def test_expired_or_tampered_session_cookie_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "test-secret")
    expired = main._session_token(int(main.time.time()) - 1)
    with pytest.raises(HTTPException) as error:
        main.require_auth(None, expired)
    assert error.value.status_code == 401
    with pytest.raises(HTTPException):
        main.require_auth(None, str(int(main.time.time()) + 3600) + ".invalid")


def test_login_sets_http_only_secure_cookie(monkeypatch):
    from fastapi import Response

    monkeypatch.setattr(main, "APP_PASSWORD", "test-secret")
    response = Response()
    result = main.dashboard_login(main.AuthLoginRequest(password="test-secret", remember=True), response)
    assert result == {"authenticated": True, "remembered": True}
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "max-age=2592000" in cookie


def test_login_rejects_wrong_password(monkeypatch):
    from fastapi import Response

    monkeypatch.setattr(main, "APP_PASSWORD", "test-secret")
    with pytest.raises(HTTPException) as error:
        main.dashboard_login(main.AuthLoginRequest(password="wrong", remember=True), Response())
    assert error.value.status_code == 401
