import asyncio
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()
from .agents import AGENTS, choose_agents
from .storage import add_message, finish_task, pause_task_for_input, set_active_agent, set_active_agents

BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("AI_MODEL", "openrouter/auto")
MAX_MODEL_ATTEMPTS = 3


async def ask_model(system_prompt: str, user_prompt: str) -> str:
    if not API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY (or OPENAI_API_KEY) is not configured.")
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    if "openrouter.ai" in BASE_URL:
        headers["HTTP-Referer"] = os.getenv("APP_PUBLIC_URL", "https://github.com/mdrafi2038-glitch/MultiAgent-AI-System")
        headers["X-Title"] = "MultiAgent AI System"
    payload = {
        "model": MODEL,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    last_error = None
    for attempt in range(MAX_MODEL_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload)
            if response.status_code in (408, 425, 429) or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("AI provider returned an empty or unsupported response.")
            return content.strip()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            last_error = exc
            retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                exc.response.status_code in (408, 425, 429) or exc.response.status_code >= 500
            )
            if not retryable or attempt == MAX_MODEL_ATTEMPTS - 1:
                raise
            await asyncio.sleep(min(2 ** attempt, 4))
    raise RuntimeError(f"AI provider request failed after retries: {last_error}")


async def clarification_check(goal: str) -> dict:
    """Decide whether essential requirements are missing before specialist work starts."""
    raw = await ask_model(
        "You are a requirements analyst for a helpful multi-agent assistant. "
        "Decide whether the user's request is clear enough to begin useful work. "
        "Ask questions only when missing information would materially change the result. "
        "For app/software requests, consider purpose, target platform, core features, design, "
        "and expected deliverables, but do not ask for details already stated or safe to assume. "
        "Ask at most 5 concise, specific questions in one grouped message. "
        "If the request is sufficiently clear, return no questions. "
        "Match the user's language (Bengali/Banglish/English). "
        "Return ONLY valid JSON with a boolean needs_clarification field, for example: "
        '{"needs_clarification":false,"questions":"","reason":"The request is clear enough"}.',
        goal,
    )
    try:
        start, end = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
        return {
            "needs_clarification": bool(parsed.get("needs_clarification")),
            "questions": str(parsed.get("questions") or "").strip(),
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        # If the provider does not follow the JSON contract, don't block the task.
        return {"needs_clarification": False, "questions": ""}


async def run_task(task_id: int, goal: str, skip_clarification: bool = False) -> dict:
    try:
        set_active_agent(task_id, "orchestrator")
        if not skip_clarification:
            check = await clarification_check(goal)
            if check["needs_clarification"] and check["questions"]:
                pause_task_for_input(task_id)
                add_message(task_id, "orchestrator", "CLARIFICATION_NEEDED:\\n" + check["questions"])
                return {"task_id": task_id, "status": "needs_input", "questions": check["questions"]}

        planner = await ask_model(
            "You are the Main Orchestrator. Return a concise execution plan as JSON with summary, steps, and specialists. "
            "Delegate only text work. Do not use tools or claim external actions.",
            goal,
        )
        add_message(task_id, "orchestrator", planner)

        specialist_keys = choose_agents(goal)
        # Publish the selected team before execution so the dashboard can show
        # every assigned specialist as IN QUEUE until it actually starts.
        add_message(task_id, "system", "AGENTS_SELECTED:" + ",".join(specialist_keys + ["reviewer"]))
        active = []
        set_active_agents(task_id, active)
        active_lock = asyncio.Lock()

        async def run_specialist(key: str):
            agent = AGENTS[key]
            async with active_lock:
                active.append(key)
                set_active_agents(task_id, active)
            prompt = (
                f"User goal:\n{goal}\n\nOrchestrator plan:\n{planner}\n\n"
                f"Collaborating team: {', '.join(AGENTS[k].name for k in specialist_keys)} plus QA Reviewer. "
                "Produce a useful specialist contribution that can be shared with the other agents. "
                "Do not claim external actions."
            )
            try:
                output = await ask_model(
                    f"You are {agent.name}, specialist in {agent.role}. {agent.instruction} "
                    "You are text-only: do not execute external actions, access accounts, or claim to have done so.",
                    prompt,
                )
                add_message(task_id, key, output)
                return {"key": key, "output": output, "error": None}
            except Exception as exc:
                note = f"{agent.name} could not complete its part after automatic retries: {type(exc).__name__}: {exc}"
                add_message(task_id, key, note)
                return {"key": key, "output": None, "error": note}
            finally:
                async with active_lock:
                    if key in active:
                        active.remove(key)
                    set_active_agents(task_id, active)

        results = await asyncio.gather(*(run_specialist(key) for key in specialist_keys))
        successful = [item for item in results if item["output"]]
        if not successful:
            raise RuntimeError("All specialist agents failed after automatic retries.")

        # Shared handoff gives each specialist's findings to the team coordinator and QA reviewer.
        team_context = "\n\n".join(
            f"### {AGENTS[item['key']].name}\n{item['output']}" for item in successful
        )
        handoff = ""
        if len(successful) > 1:
            try:
                set_active_agent(task_id, "orchestrator")
                handoff = await ask_model(
                    "You are the Agent Collaboration Coordinator. Compare all specialist contributions. "
                    "Create a concise shared handoff identifying useful cross-agent findings, dependencies, "
                    "contradictions, and gaps. Do not write the final user answer.",
                    f"Original goal:\n{goal}\n\nSpecialist contributions:\n{team_context}",
                )
                add_message(task_id, "orchestrator", "Agent-to-agent collaboration handoff:\n" + handoff)
            except Exception as exc:
                add_message(task_id, "system", f"Collaboration handoff skipped after retries: {type(exc).__name__}: {exc}")

        set_active_agent(task_id, "reviewer")
        final = await ask_model(
            "You are the QA Reviewer and final response editor. Combine specialist work into one accurate, useful answer. "
            "Use the cross-agent handoff to resolve dependencies and contradictions. Correct unsupported claims, "
            "flag uncertainty, and never claim an external action was completed.",
            f"Original request:\n{goal}\n\nSpecialist work:\n{team_context}\n\n"
            f"Cross-agent handoff:\n{handoff or 'No separate handoff was available.'}",
        )
        add_message(task_id, "reviewer", final)
        finish_task(task_id, "completed", final)
        return {
            "task_id": task_id,
            "status": "completed",
            "result": final,
            "agents": ["orchestrator"] + specialist_keys + ["reviewer"],
        }
    except Exception as exc:
        message = f"Task failed: {type(exc).__name__}: {exc}"
        try:
            add_message(task_id, "system", message)
            finish_task(task_id, "failed", message)
        except Exception:
            pass
        return {"task_id": task_id, "status": "failed", "error": message}
