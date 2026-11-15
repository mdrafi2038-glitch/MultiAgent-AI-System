"""User-configured HTTPS API plugins with explicit per-action approval."""
import ipaddress
import json
import os
import re
import socket
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import hmac


def require_auth(x_app_password: str | None = Header(default=None)):
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "Dashboard password is not configured.")
    if not x_app_password or not hmac.compare_digest(x_app_password, expected):
        raise HTTPException(401, "Incorrect or missing dashboard password.")
    return True


from .storage import connect, _sql

router = APIRouter(prefix="/api/plugins/custom", tags=["custom-plugins"])
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _public_https_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Plugin API URL must be a valid HTTPS URL without embedded credentials.")
    if parsed.port not in (None, 443):
        raise HTTPException(400, "Only standard HTTPS port 443 is supported.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
        raise HTTPException(400, "Local and internal hosts are not allowed.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise HTTPException(400, "Plugin API host could not be resolved.") from exc
    if not addresses:
        raise HTTPException(400, "Plugin API host has no public address.")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise HTTPException(400, "Plugin API host resolves to a non-public address.")
    return f"https://{host}" + (parsed.path.rstrip("/") if parsed.path else "")


class CustomPluginCreate(BaseModel):
    id: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    base_url: str = Field(min_length=9, max_length=500)
    auth_env: str | None = Field(default=None, max_length=64)
    actions: list[dict] = Field(min_length=1, max_length=30)


class PluginExecute(BaseModel):
    action_id: str = Field(min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)
    body: dict | list | None = None
    approved: bool = False


def _validate_actions(actions):
    seen = set()
    cleaned = []
    for action in actions:
        action_id = str(action.get("id", "")).strip()
        method = str(action.get("method", "GET")).upper()
        path = str(action.get("path", "")).strip()
        description = str(action.get("description", ""))[:300]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", action_id) or action_id in seen:
            raise HTTPException(400, "Every action needs a unique simple id.")
        if method not in _METHODS:
            raise HTTPException(400, f"Unsupported method for action {action_id}.")
        if not path.startswith("/") or path.startswith("//") or ".." in path or "://" in path or "\\\\" in path:
            raise HTTPException(400, f"Action {action_id} must use a safe relative API path beginning with '/'.")
        seen.add(action_id)
        cleaned.append({"id": action_id, "method": method, "path": path, "description": description})
    return cleaned


def _row(row):
    item = dict(row)
    item["actions"] = json.loads(item.pop("actions_json"))
    item.pop("auth_env", None)
    return item


@router.get("")
def list_custom_plugins(_: bool = Depends(require_auth)):
    with connect() as db:
        rows = db.execute("SELECT id,name,description,base_url,auth_env,actions_json,enabled,created_at FROM custom_plugins ORDER BY name").fetchall()
    return {"plugins": [_row(r) for r in rows]}


@router.post("", status_code=201)
def create_custom_plugin(body: CustomPluginCreate, _: bool = Depends(require_auth)):
    plugin_id = body.id.strip().lower()
    if not _ID.fullmatch(plugin_id):
        raise HTTPException(400, "Plugin id must be 2–48 lowercase letters, digits, '_' or '-'.")
    base_url = _public_https_url(body.base_url)
    auth_env = body.auth_env.strip() if body.auth_env else None
    if auth_env and not _ENV.fullmatch(auth_env):
        raise HTTPException(400, "Credential must be an environment variable name, not a secret value.")
    actions = _validate_actions(body.actions)
    with connect() as db:
        try:
            db.execute(_sql("INSERT INTO custom_plugins(id,name,description,base_url,auth_env,actions_json,enabled,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)"),
                (plugin_id, body.name.strip(), body.description.strip(), base_url, auth_env, json.dumps(actions), True, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()))
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(409, "A plugin with this id already exists.") from exc
            raise
    return {"created": True, "plugin_id": plugin_id, "message": "Plugin saved. Add its credential to the hosting environment if required."}


@router.delete("/{plugin_id}")
def delete_custom_plugin(plugin_id: str, _: bool = Depends(require_auth)):
    with connect() as db:
        cur = db.execute(_sql("DELETE FROM custom_plugins WHERE id=%s"), (plugin_id,))
    if not cur.rowcount:
        raise HTTPException(404, "Plugin not found.")
    return {"deleted": True, "plugin_id": plugin_id}


@router.post("/{plugin_id}/execute")
async def execute_custom_plugin(plugin_id: str, body: PluginExecute, _: bool = Depends(require_auth)):
    with connect() as db:
        row = db.execute(_sql("SELECT * FROM custom_plugins WHERE id=%s AND enabled=%s"), (plugin_id, True)).fetchone()
    if not row:
        raise HTTPException(404, "Enabled plugin not found.")
    plugin = dict(row)
    actions = json.loads(plugin["actions_json"])
    action = next((a for a in actions if a["id"] == body.action_id), None)
    if not action:
        raise HTTPException(404, "Action not found.")
    if action["method"] != "GET" and not body.approved:
        raise HTTPException(403, "This action changes external data. Confirm it explicitly by setting approved=true.")
    if len(json.dumps(body.params, ensure_ascii=False)) > 8000 or len(json.dumps(body.body, ensure_ascii=False)) > 20000:
        raise HTTPException(413, "Request parameters are too large.")
    base = _public_https_url(plugin["base_url"])
    target = base + action["path"]
    if urlparse(target).hostname != urlparse(base).hostname:
        raise HTTPException(400, "Action URL must stay on the plugin's configured host.")
    headers = {"Accept": "application/json"}
    env_name = plugin.get("auth_env")
    if env_name:
        token = os.getenv(env_name, "").strip()
        if not token:
            raise HTTPException(503, f"Credential environment variable {env_name} is not configured on the host.")
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            response = await client.request(action["method"], target, params=body.params, json=body.body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Plugin request failed: {type(exc).__name__}.") from exc
    content_type = response.headers.get("content-type", "")
    if response.status_code >= 400:
        raise HTTPException(502, f"Plugin API returned HTTP {response.status_code}.")
    if response.status_code in (301, 302, 303, 307, 308):
        raise HTTPException(502, "Plugin API redirects are blocked for security.")
    try:
        result = response.json() if "json" in content_type.lower() else response.text[:12000]
    except ValueError:
        result = response.text[:12000]
    return {"plugin_id": plugin_id, "action_id": body.action_id, "status_code": response.status_code, "result": result}
