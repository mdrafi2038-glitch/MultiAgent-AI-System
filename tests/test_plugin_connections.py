import pytest

from app import storage
from app import plugin_connections


def test_plugin_connection_credentials_are_encrypted_and_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "connections.db"))
    monkeypatch.setenv("APP_PASSWORD", "a-strong-test-password-123")
    storage.init_db()

    plugin_connections.save_credential("sample-api", "secret-token-value", "bearer")

    assert plugin_connections.get_credential("sample-api") == "secret-token-value"
    with storage.connect() as db:
        row = db.execute("SELECT credential_encrypted,status FROM plugin_connections WHERE plugin_id=?", ("sample-api",)).fetchone()
    assert row["status"] == "connected"
    assert row["credential_encrypted"] != "secret-token-value"
    assert plugin_connections.connection_status("sample-api")["connected"] is True


def test_disconnect_keeps_setup_state_but_clears_saved_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "connections.db"))
    monkeypatch.setenv("APP_PASSWORD", "a-strong-test-password-123")
    storage.init_db()

    plugin_connections.save_credential("sample-api", "secret-token-value")
    plugin_connections.mark_disconnected("sample-api")

    assert plugin_connections.get_credential("sample-api") is None
    assert plugin_connections.connection_status("sample-api")["status"] == "disconnected"


def test_encryption_requires_a_stable_server_secret(monkeypatch):
    monkeypatch.delenv("PLUGIN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("APP_PASSWORD", "short")
    with pytest.raises(Exception):
        plugin_connections._fernet()
