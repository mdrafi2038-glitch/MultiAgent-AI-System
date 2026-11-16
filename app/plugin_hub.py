"""User-configured HTTPS API plugins with explicit per-action approval."""
import ipaddress
import json
import os
import re
import socket
from urllib.parse import urlparse, urljoin

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import hmac
import time
from fastapi import Cookie


def require_auth(
    x_app_password: str | None = Header(default=None),
    dashboard_session: str | None = Cookie(default=None, alias="dashboard_session"),
):
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "Dashboard password is not configured.")
    if x_app_password and hmac.compare_digest(x_app_password, expected):
        return True
    # Keep custom-plugin routes consistent with the main dashboard's signed session.
    if dashboard_session and "." in dashboard_session:
        try:
            expiry_text, signature = dashboard_session.split(".", 1)
            expiry = int(expiry_text)
            now = int(time.time())
            if now < expiry <= now + 30 * 24 * 60 * 60 + 60:
                payload = f"dashboard-session:{expiry}".encode()
                expected_signature = hmac.new(expected.encode(), payload, "sha256").hexdigest()
                if hmac.compare_digest(signature, expected_signature):
                    return True
        except (TypeError, ValueError):
            pass
    raise HTTPException(401, "Incorrect or missing dashboard password.")


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


class OpenAPIDiscover(BaseModel):
    spec_url: str = Field(min_length=10, max_length=1000)


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


@router.post("/discover")
async def discover_openapi_plugin(body: OpenAPIDiscover, _: bool = Depends(require_auth)):
    """Read a public OpenAPI JSON document and return a safe, reviewable action list."""
    spec_url = body.spec_url.strip()
    parsed_spec = urlparse(spec_url)
    if parsed_spec.query or parsed_spec.fragment:
        raise HTTPException(400, "Use a direct OpenAPI JSON URL without query strings or fragments.")
    _public_https_url(spec_url)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(spec_url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not read the API documentation.") from exc
    if response.status_code in (301, 302, 303, 307, 308):
        raise HTTPException(502, "Redirects are blocked when importing plugin specifications.")
    if response.status_code >= 400:
        raise HTTPException(502, f"API documentation returned HTTP {response.status_code}.")
    if len(response.content) > 1_500_000:
        raise HTTPException(413, "OpenAPI document is too large (1.5 MB maximum).")
    try:
        spec = response.json()
    except ValueError as exc:
        raise HTTPException(400, "Only OpenAPI/Swagger JSON documents are supported.") from exc
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise HTTPException(400, "This does not look like a valid OpenAPI/Swagger document.")
    servers = spec.get("servers") or []
    server_url = servers[0].get("url") if servers and isinstance(servers[0], dict) else None
    if server_url:
        base_url = urljoin(spec_url, str(server_url))
    else:
        base_url = f"{parsed_spec.scheme}://{parsed_spec.netloc}"
    base_url = _public_https_url(base_url)
    actions = []
    used_ids = set()
    for path, methods in spec["paths"].items():
        if not isinstance(path, str) or not isinstance(methods, dict):
            continue
        # Path parameters need a richer parameter editor; skip them rather than
        # creating actions that would fail or interpolate unsafe user input.
        if "{" in path or "}" in path:
            continue
        for method, operation in methods.items():
            method = str(method).upper()
            if method not in _METHODS or not isinstance(operation, dict):
                continue
            if not path.startswith("/") or path.startswith("//") or ".." in path or "\\\\" in path:
                continue
            raw_id = operation.get("operationId") or f"{method}_{path}"
            action_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw_id)).strip("_")[:60] or f"action_{len(actions)+1}"
            base_id, suffix = action_id, 2
            while action_id in used_ids:
                action_id = f"{base_id}_{suffix}"
                suffix += 1
            used_ids.add(action_id)
            description = str(operation.get("summary") or operation.get("description") or f"{method} {path}")[:300]
            actions.append({"id": action_id, "method": method, "path": path, "description": description})
            if len(actions) >= 30:
                break
        if len(actions) >= 30:
            break
    if not actions:
        raise HTTPException(400, "No supported API operations found. This importer supports GET/POST/PUT/PATCH/DELETE paths without path parameters.")
    info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
    return {
        "name": str(info.get("title") or parsed_spec.hostname or "API Plugin")[:80],
        "description": str(info.get("description") or "Imported from an OpenAPI specification")[:500],
        "base_url": base_url,
        "actions": actions,
    }


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


class PluginEnabledUpdate(BaseModel):
    enabled: bool


@router.patch("/{plugin_id}")
def update_custom_plugin(plugin_id: str, body: PluginEnabledUpdate, _: bool = Depends(require_auth)):
    """Enable or pause a configured integration without deleting its setup."""
    with connect() as db:
        cur = db.execute(_sql("UPDATE custom_plugins SET enabled=%s WHERE id=%s"), (body.enabled, plugin_id))
    if not cur.rowcount:
        raise HTTPException(404, "Plugin not found.")
    return {"updated": True, "plugin_id": plugin_id, "enabled": body.enabled}
