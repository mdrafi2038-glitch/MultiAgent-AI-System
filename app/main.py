import hmac
import os
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
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
)
from .engine import run_task
from .agents import AGENTS

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
app = FastAPI(title="MultiAgent AI System", version="0.5.0")


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


class ClarificationRequest(BaseModel):
    response: str = Field(min_length=1, max_length=12000)


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True, "service": "multiagent-ai-system", "dashboardConfigured": bool(APP_PASSWORD)}


@app.get("/api/agents")
def agents(_: bool = Depends(require_auth)):
    team = [{"id": "orchestrator", "name": "Main Orchestrator", "role": "Task coordination and cross-agent handoff"}]
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


@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).parent.joinpath("static", "index.html").read_text(encoding="utf-8")
