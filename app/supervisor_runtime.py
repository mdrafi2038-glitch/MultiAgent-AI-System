"""Concrete, credential-gated adapters used by the Supervisor execution runtime.

Only read-only inspection runs automatically. Deployments require a separate
user approval recorded in the task conversation.
"""
import os

import httpx


def render_adapter_configured() -> bool:
    return bool(os.getenv("RENDER_API_KEY", "").strip() and os.getenv("RENDER_SERVICE_ID", "").strip())


def _render_headers() -> dict[str, str]:
    key = os.getenv("RENDER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("RENDER_API_KEY is not configured.")
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


async def inspect_render_service() -> dict:
    """Read the configured Render service and its latest deployments."""
    service_id = os.getenv("RENDER_SERVICE_ID", "").strip()
    if not render_adapter_configured():
        missing = [name for name in ("RENDER_API_KEY", "RENDER_SERVICE_ID") if not os.getenv(name, "").strip()]
        return {"state": "blocked", "integration": "render", "reason": "Missing Render configuration: " + ", ".join(missing)}
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            headers = _render_headers()
            service_response = await client.get(
                f"https://api.render.com/v1/services/{service_id}", headers=headers
            )
            service_response.raise_for_status()
            service_data = service_response.json()
            service_data = service_data.get("service", service_data) if isinstance(service_data, dict) else {}
            deploy_response = await client.get(
                f"https://api.render.com/v1/services/{service_id}/deploys",
                headers=headers,
                params={"limit": 5},
            )
            deploy_response.raise_for_status()
            deploy_rows = deploy_response.json()
        deploys = [
            row.get("deploy", row) if isinstance(row, dict) else {}
            for row in (deploy_rows if isinstance(deploy_rows, list) else [])
        ]
        latest = deploys[0] if deploys else {}
        details = service_data.get("serviceDetails") or {}
        commit = latest.get("commit") or {}
        return {
            "state": "completed",
            "integration": "render",
            "service": {
                "id": service_data.get("id"),
                "name": service_data.get("name"),
                "type": service_data.get("type"),
                "url": details.get("url"),
                "region": details.get("region"),
                "suspended": service_data.get("suspended"),
            },
            "latest_deploy": {
                "id": latest.get("id"),
                "status": latest.get("status"),
                "created_at": latest.get("createdAt"),
                "finished_at": latest.get("finishedAt"),
                "commit_id": commit.get("id") if isinstance(commit, dict) else None,
                "commit_message": commit.get("message") if isinstance(commit, dict) else None,
            },
            "deploy_count_checked": len(deploys),
        }
    except httpx.HTTPStatusError as exc:
        return {
            "state": "blocked",
            "integration": "render",
            "reason": f"Render API returned HTTP {exc.response.status_code}. Check the API key and service permissions.",
        }
    except httpx.HTTPError as exc:
        return {"state": "blocked", "integration": "render", "reason": f"Render connection failed: {type(exc).__name__}."}


async def deploy_render_service() -> dict:
    """Trigger a normal-cache deployment. Caller must enforce explicit approval."""
    service_id = os.getenv("RENDER_SERVICE_ID", "").strip()
    if not service_id or not os.getenv("RENDER_API_KEY", "").strip():
        return {"state": "blocked", "integration": "render", "reason": "RENDER_API_KEY and RENDER_SERVICE_ID are required."}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://api.render.com/v1/services/{service_id}/deploys",
                headers={**_render_headers(), "Content-Type": "application/json"},
                json={"clearCache": "do_not_clear"},
            )
            response.raise_for_status()
        data = response.json()
        data = data.get("deploy", data) if isinstance(data, dict) else {}
        return {
            "state": "accepted",
            "integration": "render",
            "deploy_id": data.get("id"),
            "deploy_status": data.get("status"),
            "message": "Render accepted the deployment request. Completion must be verified from deploy status.",
        }
    except httpx.HTTPStatusError as exc:
        return {"state": "failed", "integration": "render", "reason": f"Render rejected deployment with HTTP {exc.response.status_code}."}
    except httpx.HTTPError as exc:
        return {"state": "failed", "integration": "render", "reason": f"Render deployment request failed: {type(exc).__name__}."}
