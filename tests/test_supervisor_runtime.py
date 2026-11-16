import asyncio

from app.supervisor_runtime import (deploy_render_service, inspect_render_service, render_adapter_configured, inspect_github_repository)


def test_render_preflight_reports_missing_service(monkeypatch):
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    result = asyncio.run(inspect_render_service())
    assert result["state"] == "blocked"
    assert "RENDER_SERVICE_ID" in result["reason"]


def test_render_deploy_refuses_to_run_without_credentials(monkeypatch):
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    result = asyncio.run(deploy_render_service())
    assert result["state"] == "blocked"


def test_render_adapter_requires_both_credentials(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "configured")
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    assert render_adapter_configured() is False
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-example")
    assert render_adapter_configured() is True


def test_github_inspection_reports_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = asyncio.run(inspect_github_repository("Inspect https://github.com/example/project"))
    assert result["state"] == "blocked"
    assert "GITHUB_TOKEN" in result["reason"]


def test_github_inspection_requires_repository(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "configured")
    result = asyncio.run(inspect_github_repository("Inspect my GitHub account"))
    assert result["state"] == "blocked"
    assert "repository" in result["reason"].lower()
