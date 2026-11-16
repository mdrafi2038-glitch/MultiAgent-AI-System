import json
import hmac
import os
import re
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
import httpx
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from .storage import (
    add_message,
    create_task,
    get_task,
    init_db,
    recent_tasks,
    recover_interrupted_tasks,
    restart_task,
    resume_task_with_clarification,
    task_messages,
    create_conversation,
    list_conversations,
    conversation_messages,
    add_conversation_message,
    get_conversation,
    task_artifacts,
    get_task_artifact,
)
from .engine import run_task, ask_model
from .agents import AGENTS
from .plugin_hub import router as custom_plugins_router
from .capabilities import router as capabilities_router

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_API = "https://api.github.com"
app = FastAPI(title="MultiAgent AI System", version="0.7.0")
app.include_router(custom_plugins_router)
app.include_router(capabilities_router)


@app.on_event("startup")
def startup():
    init_db()
    recover_interrupted_tasks()


def require_auth(x_app_password: str | None = Header(default=None)):
    if not APP_PASSWORD:
        raise HTTPException(status_code=503, detail="Dashboard password is not configured. Set APP_PASSWORD in the hosting environment.")
    if not x_app_password or not hmac.compare_digest(x_app_password, APP_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect or missing dashboard password.")
    return True


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)


class ChatModeRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: int | None = None


class ClarificationRequest(BaseModel):
    response: str = Field(min_length=1, max_length=12000)


class GitHubDispatchRequest(BaseModel):
    repository: str = Field(min_length=3, max_length=200)
    workflow: str = Field(min_length=1, max_length=200)
    ref: str = Field(default="main", min_length=1, max_length=200)
    inputs: dict[str, str] = Field(default_factory=dict)


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True, "service": "multiagent-ai-system", "dashboardConfigured": bool(APP_PASSWORD)}


@app.get("/api/agents")
def agents(_: bool = Depends(require_auth)):
    team = [{"id": "supervisor", "name": "Supervisor Agent", "role": "Dependency planning, tool routing and approval control"}, {"id": "orchestrator", "name": "Main Orchestrator", "role": "Task coordination and cross-agent handoff"}, {"id": "github_agent", "name": "GitHub Agent", "role": "Repository and workflow integration (adapter-gated)"}, {"id": "render_agent", "name": "Render Agent", "role": "Hosting, logs and deployment integration (approval-gated)"}, {"id": "integration_agent", "name": "API Integration Agent", "role": "Configured API plugin integration (approval-gated)"}]
    team.extend([{"id": a.key, "name": a.name, "role": a.role} for a in AGENTS.values()])
    return team


@app.get("/api/tasks")
def tasks(_: bool = Depends(require_auth)):
    return recent_tasks()


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int, _: bool = Depends(require_auth)):
    found = get_task(task_id)
    if not found:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": found, "messages": task_messages(task_id)}


@app.get("/api/tasks/{task_id}/artifacts")
def task_artifact_list(task_id: int, _: bool = Depends(require_auth)):
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"artifacts": [
        {**item, "download_url": f"/api/tasks/{task_id}/artifacts/{item['id']}/download"}
        for item in task_artifacts(task_id)
    ]}


@app.get("/api/tasks/{task_id}/artifacts/{artifact_id}/download")
def download_task_artifact(task_id: int, artifact_id: int, _: bool = Depends(require_auth)):
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    artifact = get_task_artifact(task_id, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Project file not found")
    from fastapi.responses import Response
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", artifact["filename"])[:120]
    return Response(content=bytes(artifact["content"]), media_type=artifact["mime_type"],
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"',
                 "Content-Length": str(artifact["size_bytes"])})


@app.get("/api/conversations")
def conversations(_: bool = Depends(require_auth)):
    return list_conversations()


@app.get("/api/conversations/{conversation_id}")
def conversation_detail(conversation_id: int, _: bool = Depends(require_auth)):
    found = get_conversation(conversation_id)
    if not found:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"conversation": found, "messages": conversation_messages(conversation_id)}


@app.post("/api/chat-mode")
async def chat_mode(body: ChatModeRequest, _: bool = Depends(require_auth)):
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    conversation_id = body.conversation_id
    if conversation_id is not None and not get_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation_id is None:
        conversation_id = create_conversation(message[:70])
    history = conversation_messages(conversation_id)
    add_conversation_message(conversation_id, "user", message)
    transcript = history[-18:] + [{"role": "user", "content": message}]
    context = "\\n".join(
        ("User: " if item["role"] == "user" else "Assistant: ") + item["content"]
        for item in transcript
    )
    try:
        answer = await ask_model(
            "You are Chat mode, a conversational product-planning assistant. Reply ONLY as valid JSON with keys type and message. "
            "type must be chat, questions, or ready. For normal conversation use type=chat. "
            "When the user wants an app, website, or software built, do NOT start agents or claim work has begun. "
            "Instead gather requirements using type=questions and a questions array. Each question has question, options (3-4 choices), multiple (boolean), allow_other (boolean). "
            "Ask only useful unanswered questions and tailor them to the product, including platform, purpose, features, users, data/storage, design, login and delivery where relevant. "
            "IMPORTANT: Treat the user's submitted card answers and additional notes as actual requirements. Re-evaluate the FULL conversation after every answer submission. "
            "Do not repeat questions that have already been answered. If the core purpose, target platform, main features, and essential preferences are clear enough to begin, you MUST return type=ready now with a useful summary; do not keep asking optional questions. "
            "Use type=questions only when a genuinely essential decision is still missing. A ready response must include a non-empty summary field. "
            "Never start work yourself. Only the separate Start Building confirmation can launch agents. Match the user's language and preserve context.",
            context,
        )
    except Exception as exc:
        add_conversation_message(conversation_id, "assistant", "I couldn't get a response from the AI provider: " + str(exc)[:500])
        raise HTTPException(status_code=502, detail="Chat response failed: " + str(exc)[:300])
    def parse_chat_json(raw: str):
        candidate = raw.strip()
        candidate = re.sub(r"^\s*```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```\s*$", "", candidate)
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            first, last = candidate.find("{"), candidate.rfind("}")
            if first < 0 or last <= first:
                raise ValueError("No JSON object in model response")
            parsed = json.loads(candidate[first:last + 1])
        if not isinstance(parsed, dict) or parsed.get("type") not in {"chat", "questions", "ready"}:
            raise ValueError("Unexpected response format")
        return parsed

    try:
        payload = parse_chat_json(answer)
    except (ValueError, TypeError, json.JSONDecodeError):
        # Some providers wrap valid JSON in prose or ignore the format instruction.
        # Ask once for a strict structured repair instead of silently losing the card state.
        try:
            repaired = await ask_model(
                "Convert the supplied assistant response into ONLY valid JSON. "
                "Use exactly one of these shapes: "
                '{"type":"chat","message":"..."}, '
                '{"type":"questions","message":"...","questions":[{"question":"...","options":["...","...","..."],"multiple":false,"allow_other":true}]}, '
                'or {"type":"ready","message":"...","summary":"..."}. '
                "Preserve its meaning and language. If it is a completed app plan, choose ready; "
                "if it asks for essential missing details, choose questions. No markdown fences or extra prose.",
                "Conversation context:\n" + context + "\n\nUnstructured response to convert:\n" + answer,
            )
            payload = parse_chat_json(repaired)
        except Exception:
            payload = {"type": "chat", "message": answer}
    if payload.get("type") == "questions":
        qs = payload.get("questions")
        if not isinstance(qs, list):
            payload = {"type": "chat", "message": str(payload.get("message") or "দুঃখিত, প্রশ্নগুলো তৈরি করা যায়নি।")}
        else:
            payload["questions"] = [
                {"question": str(q.get("question", "A detail"))[:500],
                 "options": [str(x)[:160] for x in q.get("options", [])[:4]],
                 "multiple": bool(q.get("multiple", False)),
                 "allow_other": bool(q.get("allow_other", True))}
                for q in qs[:6] if isinstance(q, dict)
            ]
    if payload.get("type") == "ready" and not str(payload.get("summary", "")).strip():
        payload["summary"] = str(payload.get("message") or "Requirements gathered from this conversation.")
    answer = json.dumps(payload, ensure_ascii=False)
    add_conversation_message(conversation_id, "assistant", answer)
    return {"conversation_id": conversation_id, "answer": answer}


class StartBuildRequest(BaseModel):
    conversation_id: int


@app.post("/api/chat-mode/start-build", status_code=202)
async def start_chat_build(body: StartBuildRequest, background_tasks: BackgroundTasks, _: bool = Depends(require_auth)):
    if not get_conversation(body.conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    history = conversation_messages(body.conversation_id)
    if not history:
        raise HTTPException(status_code=400, detail="This chat has no requirements yet.")
    last_assistant = next((item for item in reversed(history) if item["role"] == "assistant"), None)
    try:
        ready_state = json.loads(last_assistant["content"]) if last_assistant else {}
    except (ValueError, TypeError):
        ready_state = {}
    if ready_state.get("type") != "ready":
        raise HTTPException(status_code=409, detail="Please finish the planning questions before starting the build.")
    transcript = "\\n".join(("USER: " if item["role"] == "user" else "ASSISTANT: ") + item["content"] for item in history)
    task_message = ("Build the app/website described in this approved Chat-mode planning conversation. "
                    "Treat the user's answers as requirements, use sensible defaults for minor gaps, and do not ask answered questions again.\\n\\n" + transcript)
    task_id = create_task(task_message)
    background_tasks.add_task(run_task, task_id, task_message)
    add_conversation_message(body.conversation_id, "assistant", "BUILD_STARTED: Agent task #" + str(task_id) + " has started.")
    return {"task_id": task_id, "status": "running"}


@app.post("/api/chat", status_code=202)
async def chat(body: ChatRequest, background_tasks: BackgroundTasks, _: bool = Depends(require_auth)):
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    task_id = create_task(message)
    background_tasks.add_task(run_task, task_id, message)
    return {"task_id": task_id, "status": "running"}


@app.post("/api/tasks/{task_id}/clarify", status_code=202)
async def clarify_task(task_id: int, body: ClarificationRequest, background_tasks: BackgroundTasks, _: bool = Depends(require_auth)):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "needs_input":
        raise HTTPException(status_code=409, detail="This task is not waiting for clarification.")
    response = body.response.strip()
    if not response:
        raise HTTPException(status_code=400, detail="Please provide your clarification.")
    if not resume_task_with_clarification(task_id, response):
        raise HTTPException(status_code=409, detail="Task state changed. Refresh and try again.")
    add_message(task_id, "user", response)
    updated = get_task(task_id)
    background_tasks.add_task(run_task, task_id, updated["user_message"], True)
    return {"task_id": task_id, "status": "running"}


@app.post("/api/tasks/{task_id}/retry", status_code=202)
async def retry_task(task_id: int, background_tasks: BackgroundTasks, _: bool = Depends(require_auth)):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "failed":
        raise HTTPException(status_code=409, detail="Only failed tasks can be retried.")
    if not restart_task(task_id):
        raise HTTPException(status_code=409, detail="Task state changed. Refresh and try again.")
    add_message(task_id, "system", "Retry started by the dashboard.")
    background_tasks.add_task(run_task, task_id, task["user_message"])
    return {"task_id": task_id, "status": "running"}


def github_headers():
    if not GITHUB_TOKEN:
        raise HTTPException(status_code=503, detail="GitHub plugin is not connected. Add GITHUB_TOKEN in the Render service Environment settings.")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def validate_github_repository(repository: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise HTTPException(status_code=400, detail="Repository must use owner/repository format.")


async def github_api(method: str, path: str, *, params=None, payload=None, follow_redirects=True):
    headers = github_headers()
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=follow_redirects) as client:
            response = await client.request(method, f"{GITHUB_API}{path}", headers=headers, params=params, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub connection failed: {type(exc).__name__}.") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("message", "GitHub API request failed.")
        except ValueError:
            detail = "GitHub API request failed."
        raise HTTPException(status_code=response.status_code, detail=f"GitHub: {detail}")
    return response


@app.get("/api/plugins")
def plugins(_: bool = Depends(require_auth)):
    return {
        "plugins": [
            {
                "id": "github",
                "name": "GitHub",
                "description": "Browse repositories, trigger GitHub Actions workflows, monitor builds, and retrieve build artifacts.",
                "connected": bool(GITHUB_TOKEN),
                "capabilities": ["repositories", "workflows", "workflow_dispatch", "build_status", "artifacts"],
            }
        ]
    }


@app.get("/api/plugins/github/workflows")
async def github_workflows(repository: str, _: bool = Depends(require_auth)):
    validate_github_repository(repository)
    response = await github_api("GET", f"/repos/{repository}/actions/workflows", params={"per_page": 100})
    data = response.json()
    return {
        "workflows": [
            {"id": item["id"], "name": item.get("name") or item.get("path"), "path": item.get("path"), "state": item.get("state")}
            for item in data.get("workflows", [])
        ]
    }


@app.post("/api/plugins/github/dispatch", status_code=202)
async def github_dispatch(body: GitHubDispatchRequest, _: bool = Depends(require_auth)):
    validate_github_repository(body.repository)
    if not re.fullmatch(r"(?:[0-9]+|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.ya?ml)", body.workflow):
        raise HTTPException(status_code=400, detail="Choose a workflow ID or a workflow .yml/.yaml path.")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", body.ref) or ".." in body.ref:
        raise HTTPException(status_code=400, detail="Invalid Git ref.")
    if len(body.inputs) > 20 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", key) or len(value) > 1000 for key, value in body.inputs.items()):
        raise HTTPException(status_code=400, detail="Workflow inputs are invalid or too large.")
    response = await github_api(
        "POST",
        f"/repos/{body.repository}/actions/workflows/{body.workflow}/dispatches",
        payload={"ref": body.ref, "inputs": body.inputs},
    )
    return {"accepted": response.status_code == 204, "repository": body.repository, "workflow": body.workflow, "ref": body.ref, "message": "GitHub Actions workflow dispatch accepted."}


@app.get("/api/plugins/github/runs")
async def github_runs(repository: str, branch: str | None = None, _: bool = Depends(require_auth)):
    validate_github_repository(repository)
    params = {"per_page": 10}
    if branch:
        params["branch"] = branch
    response = await github_api("GET", f"/repos/{repository}/actions/runs", params=params)
    return {
        "runs": [
            {
                "id": item["id"], "name": item.get("name"), "run_number": item.get("run_number"),
                "status": item.get("status"), "conclusion": item.get("conclusion"),
                "head_branch": item.get("head_branch"), "created_at": item.get("created_at"),
                "html_url": item.get("html_url"),
            }
            for item in response.json().get("workflow_runs", [])
        ]
    }


@app.get("/api/plugins/github/runs/{run_id}/artifacts")
async def github_artifacts(run_id: int, repository: str, _: bool = Depends(require_auth)):
    validate_github_repository(repository)
    response = await github_api("GET", f"/repos/{repository}/actions/runs/{run_id}/artifacts")
    return {
        "artifacts": [
            {"id": item["id"], "name": item.get("name"), "size_in_bytes": item.get("size_in_bytes"), "expired": item.get("expired")}
            for item in response.json().get("artifacts", [])
        ]
    }


@app.get("/api/plugins/github/artifacts/{artifact_id}/download")
async def github_artifact_download(artifact_id: int, repository: str, _: bool = Depends(require_auth)):
    validate_github_repository(repository)
    response = await github_api(
        "GET", f"/repos/{repository}/actions/artifacts/{artifact_id}/zip", follow_redirects=False
    )
    location = response.headers.get("location")
    if not location:
        raise HTTPException(status_code=502, detail="GitHub did not provide an artifact download URL.")
    return RedirectResponse(location, status_code=302)


@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).parent.joinpath("static", "index.html").read_text(encoding="utf-8")
