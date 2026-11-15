"""Optional capability adapters for search, media, files, browser and connectors.

All credentials are read server-side from environment variables and never returned.
Browser actions are restricted to explicitly configured public hostnames.
"""
import csv
import io
import ipaddress
import json
import os
import socket
import zipfile
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
import hmac

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])
MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".pdf", ".docx"}
MUTATING_BROWSER_ACTIONS = {"click", "fill", "select", "submit"}


def require_auth(x_app_password: str | None = Header(default=None)):
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "Dashboard password is not configured.")
    if not x_app_password or not hmac.compare_digest(x_app_password, expected):
        raise HTTPException(401, "Incorrect or missing dashboard password.")
    return True


def _configured(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def capability_status() -> dict:
    return {
        "web_search": {"configured": _configured("TAVILY_API_KEY"), "provider": "Tavily", "required_env": ["TAVILY_API_KEY"]},
        "image_generation": {"configured": _configured("IMAGE_API_KEY") and _configured("IMAGE_MODEL"), "provider": os.getenv("IMAGE_PROVIDER", "OpenAI-compatible"), "required_env": ["IMAGE_API_KEY", "IMAGE_MODEL", "IMAGE_API_BASE_URL (optional)"]},
        "video_generation": {"configured": _configured("VIDEO_API_URL") and _configured("VIDEO_API_KEY"), "provider": os.getenv("VIDEO_PROVIDER", "Custom video API"), "required_env": ["VIDEO_API_URL", "VIDEO_API_KEY"]},
        "file_analysis": {"configured": True, "provider": "Built-in text/PDF/DOCX extractor", "required_env": []},
        "browser_automation": {"configured": _configured("BROWSERLESS_URL") and _configured("BROWSERLESS_TOKEN") and _configured("BROWSER_ALLOWED_HOSTS"), "provider": "Browserless-compatible remote browser", "required_env": ["BROWSERLESS_URL", "BROWSERLESS_TOKEN", "BROWSER_ALLOWED_HOSTS"]},
        "github": {"configured": _configured("GITHUB_TOKEN"), "provider": "GitHub API", "required_env": ["GITHUB_TOKEN"]},
        "render": {"configured": _configured("RENDER_API_KEY"), "provider": "Render API", "required_env": ["RENDER_API_KEY"]},
        "custom_api_plugins": {"configured": True, "provider": "Universal API Plugin Hub", "required_env": []},
    }


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    max_results: int = Field(default=5, ge=1, le=10)


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=4000)
    size: str = Field(default="1024x1024", pattern=r"^(256x256|512x512|1024x1024|1536x1024|1024x1536)$")


class VideoRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=4000)
    duration_seconds: int = Field(default=5, ge=1, le=30)
    aspect_ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16|1:1)$")


class BrowserAction(BaseModel):
    action: str = Field(pattern=r"^(goto|click|fill|select|text|wait)$")
    selector: str | None = Field(default=None, max_length=500)
    value: str | None = Field(default=None, max_length=2000)


class BrowserRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    actions: list[BrowserAction] = Field(min_length=1, max_length=15)
    approved: bool = False


def _check_public_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Only public HTTP/HTTPS URLs without embedded credentials are allowed.")
    host = parsed.hostname.rstrip(".").lower()
    allowed = {x.strip().lower() for x in os.getenv("BROWSER_ALLOWED_HOSTS", "").split(",") if x.strip()}
    if not allowed or not any(host == item or host.endswith("." + item) for item in allowed):
        raise HTTPException(403, "This website is not in BROWSER_ALLOWED_HOSTS.")
    try:
        for result in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(result[4][0])
            if not address.is_global:
                raise HTTPException(400, "Private or internal network destinations are blocked.")
    except socket.gaierror as exc:
        raise HTTPException(400, "Website hostname could not be resolved.") from exc
    return value


@router.get("/status")
def status(_: bool = Depends(require_auth)):
    return {"capabilities": capability_status(), "secrets_exposed": False}


@router.post("/web-search")
async def web_search(body: SearchRequest, _: bool = Depends(require_auth)):
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Web Search is not configured. Add TAVILY_API_KEY in Render Environment.")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                os.getenv("TAVILY_API_URL", "https://api.tavily.com/search"),
                json={"api_key": key, "query": body.query, "max_results": body.max_results, "search_depth": "advanced"},
            )
        response.raise_for_status()
        data = response.json()
        return {"query": body.query, "answer": data.get("answer"), "results": [
            {"title": x.get("title"), "url": x.get("url"), "content": x.get("content"), "score": x.get("score")}
            for x in data.get("results", [])
        ]}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Search provider request failed: {type(exc).__name__}.") from exc


@router.post("/image-generation")
async def image_generation(body: ImageRequest, _: bool = Depends(require_auth)):
    key = os.getenv("IMAGE_API_KEY", "").strip()
    model = os.getenv("IMAGE_MODEL", "").strip()
    base = os.getenv("IMAGE_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if not key or not model:
        raise HTTPException(503, "Image generation needs IMAGE_API_KEY and IMAGE_MODEL in Render Environment.")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                base + "/images/generations",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "prompt": body.prompt, "size": body.size, "n": 1},
            )
        response.raise_for_status()
        data = response.json()
        images = data.get("data") or []
        if not images:
            raise HTTPException(502, "Image provider returned no image.")
        item = images[0]
        return {"created": True, "url": item.get("url"), "revised_prompt": item.get("revised_prompt"),
                "b64_json": item.get("b64_json"), "provider": os.getenv("IMAGE_PROVIDER", "OpenAI-compatible")}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Image provider request failed: {type(exc).__name__}.") from exc


@router.post("/video-generation")
async def video_generation(body: VideoRequest, _: bool = Depends(require_auth)):
    endpoint = os.getenv("VIDEO_API_URL", "").strip()
    key = os.getenv("VIDEO_API_KEY", "").strip()
    if not endpoint or not key:
        raise HTTPException(503, "Video generation needs VIDEO_API_URL and VIDEO_API_KEY in Render Environment.")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(400, "VIDEO_API_URL must be an HTTPS endpoint.")
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"prompt": body.prompt, "duration_seconds": body.duration_seconds, "aspect_ratio": body.aspect_ratio})
        response.raise_for_status()
        return {"submitted": True, "provider": os.getenv("VIDEO_PROVIDER", "Custom video API"),
                "result": response.json(), "note": "Video job response is provider-specific; use its returned task/status URL to track rendering."}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Video provider request failed: {type(exc).__name__}.") from exc


def _extract_docx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "replace")
    import re
    return re.sub(r"<[^>]+>", " ", xml).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

def _extract_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise HTTPException(503, "PDF support is not installed yet.") from exc
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages[:100])


@router.post("/file-analysis")
async def file_analysis(file: UploadFile = File(...), _: bool = Depends(require_auth)):
    name = (file.filename or "upload").replace("\\", "/").split("/")[-1]
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in {".txt", ".md", ".csv", ".json", ".pdf", ".docx"}:
        raise HTTPException(415, "Supported formats: TXT, MD, CSV, JSON, PDF and DOCX.")
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "Maximum upload size is 10 MB.")
    if ext == ".pdf":
        content = _extract_pdf(raw)
    elif ext == ".docx":
        content = _extract_docx(raw)
    elif ext == ".csv":
        text = raw.decode("utf-8-sig", "replace")
        content = "\n".join(",".join(row) for row in csv.reader(io.StringIO(text)))
    else:
        content = raw.decode("utf-8-sig", "replace")
        if ext == ".json":
            try:
                content = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
            except ValueError:
                raise HTTPException(400, "The uploaded JSON file is invalid.")
    return {"filename": name, "content_type": file.content_type, "characters": len(content),
            "text": content[:100000], "truncated": len(content) > 100000,
            "note": "Extracted text is ready for AI analysis. This endpoint does not execute macros or embedded files."}


@router.post("/browser")
async def browser_automation(body: BrowserRequest, _: bool = Depends(require_auth)):
    endpoint = os.getenv("BROWSERLESS_URL", "").strip()
    token = os.getenv("BROWSERLESS_TOKEN", "").strip()
    if not endpoint or not token or not os.getenv("BROWSER_ALLOWED_HOSTS", "").strip():
        raise HTTPException(503, "Browser automation needs BROWSERLESS_URL, BROWSERLESS_TOKEN and BROWSER_ALLOWED_HOSTS.")
    if any(action.action in MUTATING_BROWSER_ACTIONS for action in body.actions) and not body.approved:
        raise HTTPException(409, "This browser task may change website data. Explicit approval is required.")
    target = _check_public_url(body.url)
    for action in body.actions:
        if action.action in {"click", "fill", "select"} and not action.selector:
            raise HTTPException(400, f"{action.action} requires a selector.")
        if action.action in {"fill", "select"} and action.value is None:
            raise HTTPException(400, f"{action.action} requires a value.")
    # Browserless /function runs a fixed, constrained Playwright action list.
    actions_json = json.dumps([a.model_dump() for a in body.actions])
    start_url = json.dumps(target)
    script = ("module.exports = async ({ page }) => {"
        f"const actions = {actions_json}; await page.goto({start_url}, {{waitUntil:'domcontentloaded', timeout:30000}});"
        "const out=[]; for (const a of actions) {"
        "if(a.action==='goto'){await page.goto(a.value,{waitUntil:'domcontentloaded',timeout:30000});}"
        "else if(a.action==='click'){await page.locator(a.selector).first().click({timeout:10000});}"
        "else if(a.action==='fill'){await page.locator(a.selector).first().fill(a.value,{timeout:10000});}"
        "else if(a.action==='select'){await page.locator(a.selector).first().selectOption(a.value,{timeout:10000});}"
        "else if(a.action==='wait'){await page.waitForTimeout(Math.min(Number(a.value)||500,3000));}"
        "else if(a.action==='text'){out.push((await page.locator(a.selector||'body').innerText({timeout:10000})).slice(0,12000));}"
        "} return {url:page.url(),title:await page.title(),extracted:out}; };")
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(endpoint, params={"token": token}, content=script,
                headers={"Content-Type": "application/javascript"})
        response.raise_for_status()
        return {"completed": True, "result": response.json(), "mutating_actions_approved": body.approved}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Remote browser request failed: {type(exc).__name__}.") from exc
