import asyncio

from app.supervisor_runtime import deploy_render_service, inspect_render_service, render_adapter_configured


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
