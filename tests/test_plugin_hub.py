import pytest
from fastapi import HTTPException

from app import storage
from app.plugin_hub import _public_https_url, _validate_actions


def test_plugin_actions_require_unique_ids_and_relative_paths():
    actions = _validate_actions([
        {"id": "read", "method": "GET", "path": "/v1/status", "description": "Read status"},
        {"id": "create", "method": "POST", "path": "/v1/items", "description": "Create item"},
    ])
    assert [item["id"] for item in actions] == ["read", "create"]


@pytest.mark.parametrize("actions", [
    [{"id": "same", "method": "GET", "path": "/a"}, {"id": "same", "method": "GET", "path": "/b"}],
    [{"id": "bad", "method": "TRACE", "path": "/a"}],
    [{"id": "bad", "method": "POST", "path": "https://evil.example/steal"}],
    [{"id": "bad", "method": "GET", "path": "/../admin"}],
])
def test_plugin_action_allowlist_rejects_unsafe_definitions(actions):
    with pytest.raises(HTTPException):
        _validate_actions(actions)


def test_plugin_url_rejects_local_and_non_https_hosts():
    with pytest.raises(HTTPException):
        _public_https_url("http://example.com/api")
    with pytest.raises(HTTPException):
        _public_https_url("https://localhost/api")


def test_custom_plugin_table_is_created_in_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "plugins.db"))
    storage.init_db()
    with storage.connect() as db:
        row = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='custom_plugins'").fetchone()
    assert row is not None
