import os
import httpx
from dotenv import load_dotenv

load_dotenv()
from .agents import AGENTS, choose_agents
from .storage import add_message, finish_task, set_active_agent

BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("AI_MODEL", "openrouter/auto")

async def ask_model(system_prompt: str, user_prompt: str) -> str:
    if not API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured. Add a compatible provider key in hosting environment settings.")
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    if "openrouter.ai" in BASE_URL:
        headers["HTTP-Referer"] = os.getenv("APP_PUBLIC_URL", "https://github.com/mdrafi2038-glitch/MultiAgent-AI-System")
        headers["X-Title"] = "MultiAgent AI System"
    payload = {"model": MODEL, "temperature": 0.3, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]}
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    return data["choices"][0]["message"]["content"].strip()

async def run_task(task_id: int, goal: str) -> dict:
    try:
        set_active_agent(task_id, "orchestrator")
        planner = await ask_model("You are the Main Orchestrator. Return a concise execution plan as JSON with summary, steps, and specialists. Do not use tools or claim external actions.", goal)
        add_message(task_id, "orchestrator", planner)
        specialist_keys = choose_agents(goal)
        outputs = []
        for key in specialist_keys:
            set_active_agent(task_id, key)
            agent = AGENTS[key]
            prompt = f"User goal:\n{goal}\n\nOrchestrator plan:\n{planner}"
            output = await ask_model(f"You are {agent.name}, specialist in {agent.role}. {agent.instruction} You are text-only: do not execute external actions, access accounts, or claim to have done so. Ask for approval before proposing consequential external actions.", prompt)
            add_message(task_id, key, output)
            outputs.append(f"### {agent.name}\n{output}")
        set_active_agent(task_id, "reviewer")
        review_input = "\n\n".join(outputs)
        final = await ask_model("You are the QA Reviewer and final response editor. Combine specialist work into one accurate, useful answer. Correct contradictions, flag uncertainty, and never claim an external action was completed.", f"Original request:\n{goal}\n\nSpecialist work:\n{review_input}")
        add_message(task_id, "reviewer", final)
        finish_task(task_id, "completed", final)
        return {"task_id": task_id, "status": "completed", "result": final, "agents": ["orchestrator"] + specialist_keys + ["reviewer"]}
    except Exception as exc:
        message = f"Task failed: {type(exc).__name__}: {exc}"
        add_message(task_id, "system", message)
        finish_task(task_id, "failed", message)
        return {"task_id": task_id, "status": "failed", "error": message}
