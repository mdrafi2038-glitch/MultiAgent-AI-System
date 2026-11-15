from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from .storage import init_db, create_task, recent_tasks, task_messages
from .engine import run_task
from .agents import AGENTS

load_dotenv()
app = FastAPI(title="MultiAgent AI System", version="0.1.0")

@app.on_event("startup")
def startup():
    init_db()

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)

@app.get("/health")
def health():
    return {"ok": True, "service": "multiagent-ai-system"}

@app.get("/api/agents")
def agents():
    return [{"id": a.key, "name": a.name, "role": a.role} for a in AGENTS.values()]

@app.get("/api/tasks")
def tasks():
    return recent_tasks()

@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int):
    found = [t for t in recent_tasks(100) if t["id"] == task_id]
    if not found:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": found[0], "messages": task_messages(task_id)}

@app.post("/api/chat")
async def chat(body: ChatRequest):
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    task_id = create_task(message)
    return await run_task(task_id, message)

@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).parent.joinpath("static", "index.html").read_text(encoding="utf-8")
