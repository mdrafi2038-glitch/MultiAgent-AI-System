import os

from app.capabilities import capability_status


def test_capability_status_reports_configuration_without_secrets(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "secret-search-token")
    monkeypatch.setenv("IMAGE_API_KEY", "secret-image-token")
    monkeypatch.setenv("IMAGE_MODEL", "image-model")
    monkeypatch.setenv("RENDER_API_KEY", "secret-render-token")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-example")
    status = capability_status()
    assert status["web_search"]["configured"] is True
    assert status["image_generation"]["configured"] is True
    assert status["render"]["configured"] is True
    assert "secret-search-token" not in repr(status)
    assert "secret-image-token" not in repr(status)
    assert "secret-render-token" not in repr(status)


def test_capability_status_marks_optional_providers_unconfigured(monkeypatch):
    for name in ("TAVILY_API_KEY", "IMAGE_API_KEY", "IMAGE_MODEL", "VIDEO_API_URL",
                 "VIDEO_API_KEY", "BROWSERLESS_URL", "BROWSERLESS_TOKEN",
                 "BROWSER_ALLOWED_HOSTS", "RENDER_API_KEY", "RENDER_SERVICE_ID"):
        monkeypatch.delenv(name, raising=False)
    status = capability_status()
    assert status["web_search"]["configured"] is False
    assert status["image_generation"]["configured"] is False
    assert status["video_generation"]["configured"] is False
    assert status["browser_automation"]["configured"] is False
    assert status["render"]["configured"] is False
