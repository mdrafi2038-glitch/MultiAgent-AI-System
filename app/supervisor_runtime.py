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


def _github_repository_from_text(goal: str) -> str | None:
    import re

    match = re.search(r"github\\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", goal, re.IGNORECASE)
    if match:
        return f"{match.group(1)}/{match.group(2).removesuffix('.git')}"
    match = re.search(r"(?:repo(?:sitory)?\\s*[:=]?\\s*)([A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100})", goal, re.IGNORECASE)
    return match.group(1) if match else None


async def inspect_github_repository(goal: str) -> dict:
    """Read repository metadata, workflows and recent runs; never writes to GitHub."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repository = _github_repository_from_text(goal)
    if not token:
        return {"state": "blocked", "integration": "github", "reason": "GITHUB_TOKEN is not configured."}
    if not repository:
        return {"state": "blocked", "integration": "github", "reason": "Please include a GitHub repository URL or owner/repository name."}
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}", repository):
        return {"state": "blocked", "integration": "github", "reason": "Repository name is invalid."}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            base = f"https://api.github.com/repos/{repository}"
            repo_response = await client.get(base, headers=headers)
            repo_response.raise_for_status()
            repo_data = repo_response.json()
            workflows_response = await client.get(base + "/actions/workflows", headers=headers, params={"per_page": 30})
            workflows_response.raise_for_status()
            runs_response = await client.get(base + "/actions/runs", headers=headers, params={"per_page": 5})
            runs_response.raise_for_status()
        workflows = workflows_response.json().get("workflows", [])
        runs = runs_response.json().get("workflow_runs", [])
        latest = runs[0] if runs else {}
        return {
            "state": "completed",
            "integration": "github",
            "repository": repository,
            "default_branch": repo_data.get("default_branch"),
            "private": repo_data.get("private"),
            "description": repo_data.get("description"),
            "html_url": repo_data.get("html_url"),
            "workflow_count": len(workflows),
            "workflows": [
                {"name": item.get("name"), "path": item.get("path"), "state": item.get("state")}
                for item in workflows[:15]
            ],
            "latest_workflow_run": {
                "name": latest.get("name"),
                "status": latest.get("status"),
                "conclusion": latest.get("conclusion"),
                "branch": latest.get("head_branch"),
                "html_url": latest.get("html_url"),
            } if latest else None,
        }
    except httpx.HTTPStatusError as exc:
        return {
            "state": "blocked",
            "integration": "github",
            "repository": repository,
            "reason": f"GitHub API returned HTTP {exc.response.status_code}. Check token scope and repository access.",
        }
    except httpx.HTTPError as exc:
        return {"state": "blocked", "integration": "github", "repository": repository, "reason": f"GitHub connection failed: {type(exc).__name__}."}
