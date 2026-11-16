"""Secure connect/disconnect lifecycle for dashboard integrations."""
import base64
import hashlib
import os
from datetime import datetime, timezone

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .storage import connect, _sql
from fastapi import Header, Cookie
import hmac
import time


def require_auth(x_app_password: str | None = Header(default=None), dashboard_session: str | None = Cookie(default=None, alias="dashboard_session")):
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "Dashboard password is not configured.")
    if x_app_password and hmac.compare_digest(x_app_password, expected):
        return True
    if dashboard_session and "." in dashboard_session:
        try:
            expiry_text, signature = dashboard_session.split(".", 1)
            expiry = int(expiry_text)
            payload = f"dashboard-session:{expiry}".encode()
            expected_signature = hmac.new(expected.encode(), payload, "sha256").hexdigest()
            if int(time.time()) < expiry <= int(time.time()) + 30 * 24 * 60 * 60 + 60 and hmac.compare_digest(signature, expected_signature):
                return True
        except (TypeError, ValueError):
            pass
    raise HTTPException(401, "Incorrect or missing dashboard password.")

router = APIRouter(prefix="/api/plugins", tags=["plugin-connections"])


def _fernet():
    secret = os.getenv("PLUGIN_ENCRYPTION_KEY", "").strip() or os.getenv("APP_PASSWORD", "")
    if len(secret) < 16:
        raise HTTPException(503, "Set PLUGIN_ENCRYPTION_KEY (or a strong APP_PASSWORD) before connecting plugins.")
    key = base64.urlsafe_b64encode(hashlib.sha256(("multiagent-plugin-v1:" + secret).encode()).digest())
    return Fernet(key)


def save_credential(plugin_id: str, token: str | None, auth_type: str = "bearer"):
    encrypted = _fernet().encrypt(token.encode()).decode() if token else None
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute(_sql("""
            INSERT INTO plugin_connections(plugin_id,credential_encrypted,auth_type,status,updated_at)
            VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(plugin_id) DO UPDATE SET credential_encrypted=excluded.credential_encrypted,
            auth_type=excluded.auth_type,status=excluded.status,updated_at=excluded.updated_at
        """), (plugin_id, encrypted, auth_type, "connected", now))


def mark_disconnected(plugin_id: str):
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute(_sql("""
            INSERT INTO plugin_connections(plugin_id,credential_encrypted,auth_type,status,updated_at)
            VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(plugin_id) DO UPDATE SET credential_encrypted=excluded.credential_encrypted,
            status=excluded.status,updated_at=excluded.updated_at
        """), (plugin_id, None, "bearer", "disconnected", now))
        if plugin_id != "github":
            db.execute(_sql("UPDATE custom_plugins SET enabled=%s WHERE id=%s"), (False, plugin_id))


def get_auth_type(plugin_id: str) -> str:
    with connect() as db:
        row = db.execute(_sql("SELECT auth_type FROM plugin_connections WHERE plugin_id=%s AND status=%s"), (plugin_id, "connected")).fetchone()
    return row["auth_type"] if row else "bearer"


def get_credential(plugin_id: str) -> str | None:
    with connect() as db:
        row = db.execute(_sql("SELECT credential_encrypted FROM plugin_connections WHERE plugin_id=%s AND status=%s"),
                         (plugin_id, "connected")).fetchone()
    if not row or not row["credential_encrypted"]:
        return None
    try:
        return _fernet().decrypt(row["credential_encrypted"].encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(503, "Saved plugin credential cannot be decrypted. Reconnect this integration.") from exc


def connection_status(plugin_id: str) -> dict:
    with connect() as db:
        row = db.execute(_sql("SELECT auth_type,status,updated_at,credential_encrypted FROM plugin_connections WHERE plugin_id=%s"),
                         (plugin_id,)).fetchone()
    if not row:
        return {"connected": False, "status": "unconfigured", "auth_type": None, "updated_at": None}
    return {"connected": row["status"] == "connected", "status": row["status"],
            "auth_type": row["auth_type"], "updated_at": row["updated_at"],
            "has_credential": bool(row["credential_encrypted"])}


class ConnectRequest(BaseModel):
    credential: str = Field(default="", max_length=4000)
    auth_type: str = Field(default="bearer", pattern="^(bearer|api_key)$")


@router.get("/connections")
def list_connections(_: bool = Depends(require_auth)):
    with connect() as db:
        rows = db.execute("SELECT plugin_id,auth_type,status,updated_at,credential_encrypted FROM plugin_connections ORDER BY plugin_id").fetchall()
    result = {r["plugin_id"]: {"connected": r["status"] == "connected", "status": r["status"],
              "auth_type": r["auth_type"], "updated_at": r["updated_at"],
              "has_credential": bool(r["credential_encrypted"])} for r in rows}
    # Preserve legacy environment-based connections until users explicitly migrate.
    result.setdefault("github", {"connected": bool(os.getenv("GITHUB_TOKEN", "").strip()),
        "status": "connected" if os.getenv("GITHUB_TOKEN", "").strip() else "disconnected",
        "auth_type": "environment", "updated_at": None, "has_credential": bool(os.getenv("GITHUB_TOKEN", "").strip())})
    with connect() as db:
        plugins = db.execute("SELECT id,enabled,auth_env FROM custom_plugins").fetchall()
    for plugin in plugins:
        if plugin["id"] not in result:
            legacy = bool(plugin["auth_env"] and os.getenv(plugin["auth_env"], "").strip())
            connected = bool(plugin["enabled"] and (legacy or not plugin["auth_env"]))
            result[plugin["id"]] = {"connected": connected, "status": "connected" if connected else "disconnected",
                "auth_type": "environment" if legacy else None, "updated_at": None, "has_credential": legacy}
    return {"connections": result}


@router.put("/{plugin_id}/connection")
async def connect_plugin(plugin_id: str, body: ConnectRequest, _: bool = Depends(require_auth)):
    token = body.credential.strip()
    if plugin_id == "github":
        if body.auth_type != "bearer":
            raise HTTPException(400, "GitHub connections use Bearer tokens.")
        if not token:
            raise HTTPException(400, "Enter a GitHub fine-grained personal access token.")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.get("https://api.github.com/user",
                    headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                             "X-GitHub-Api-Version": "2022-11-28"})
            if response.status_code != 200:
                mark_disconnected(plugin_id)
                raise HTTPException(401, "GitHub did not accept this token. Check it and reconnect.")
            profile = response.json()
        except httpx.HTTPError as exc:
            mark_disconnected(plugin_id)
            raise HTTPException(502, "Could not verify GitHub connection: " + type(exc).__name__) from exc
        save_credential(plugin_id, token, body.auth_type)
        return {"connected": True, "plugin_id": plugin_id, "account": profile.get("login"),
                "message": "GitHub connected successfully."}
    # A custom integration must exist before it can be connected.
    with connect() as db:
        row = db.execute(_sql("SELECT base_url,actions_json,auth_env FROM custom_plugins WHERE id=%s"), (plugin_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Plugin configuration not found.")
    if not token and row["auth_env"]:
        token = os.getenv(row["auth_env"], "").strip()
    if not token and row["auth_env"]:
        raise HTTPException(400, "This integration requires an API credential.")
    if token:
        save_credential(plugin_id, token, body.auth_type)
    else:
        save_credential(plugin_id, None, "none")
    # If the plugin exposes a GET health/read action, verify the credential before marking it connected.
    import json
    actions = json.loads(row["actions_json"])
    read_action = next((a for a in actions if a["method"] == "GET"), None)
    if read_action:
        headers = {"Accept": "application/json"}
        if token:
            if body.auth_type == "bearer":
                headers["Authorization"] = "Bearer " + token
            else:
                headers["X-API-Key"] = token
        try:
            async with httpx.AsyncClient(timeout=18, follow_redirects=False) as client:
                response = await client.get(row["base_url"] + read_action["path"], headers=headers)
            if response.status_code in (401, 403):
                mark_disconnected(plugin_id)
                raise HTTPException(401, "The service rejected this credential. Check it and reconnect.")
            if response.status_code in (301, 302, 303, 307, 308):
                mark_disconnected(plugin_id)
                raise HTTPException(502, "The service redirected the connection check; redirects are blocked.")
        except httpx.HTTPError as exc:
            mark_disconnected(plugin_id)
            raise HTTPException(502, "Could not verify this integration: " + type(exc).__name__) from exc
    with connect() as db:
        db.execute(_sql("UPDATE custom_plugins SET enabled=%s WHERE id=%s"), (True, plugin_id))
    return {"connected": True, "plugin_id": plugin_id, "message": "Integration connected successfully."}


@router.delete("/{plugin_id}/connection")
def disconnect_plugin(plugin_id: str, _: bool = Depends(require_auth)):
    mark_disconnected(plugin_id)
    return {"disconnected": True, "plugin_id": plugin_id, "message": "Integration disconnected. Its setup is kept so you can reconnect later."}
