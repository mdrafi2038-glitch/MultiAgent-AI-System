import asyncio
import json
import os
import re
import random
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()
from .agents import AGENTS, choose_agents
from .supervisor import build_supervisor_plan, validate_plan, render_deploy_requested
from .supervisor_runtime import inspect_render_service, deploy_render_service
from .storage import add_message, finish_task, pause_task_for_input, set_active_agent, set_active_agents, save_task_artifact, task_messages
from .artifacts import create_project_zip

BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("AI_MODEL", "openrouter/auto")
MAX_MODEL_ATTEMPTS = int(os.getenv("AI_MAX_MODEL_ATTEMPTS", "5"))
MODEL_REQUEST_TIMEOUT = float(os.getenv('AI_REQUEST_TIMEOUT_SECONDS', '40'))
MODEL_REQUEST_CONCURRENCY = max(1, int(os.getenv("AI_REQUEST_CONCURRENCY", "1")))
MODEL_MIN_REQUEST_INTERVAL = max(0.0, float(os.getenv("AI_MIN_REQUEST_INTERVAL_SECONDS", "1.5")))
_MODEL_REQUEST_SEMAPHORE = asyncio.Semaphore(MODEL_REQUEST_CONCURRENCY)
_MODEL_REQUEST_LOCK = asyncio.Lock()
_MODEL_LAST_REQUEST_AT = 0.0


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


async def ask_model_with_recovery(task_id: int, agent: str, stage: str,
                                  system_prompt: str, user_prompt: str,
                                  max_recoveries: int | None = None) -> str:
    """Keep recovering from transient failures until the stage succeeds or is truly blocked."""
    attempt = 0
    recovery_notes = ""
    last_error = None
    while True:
        if attempt:
            report_progress(
                task_id,
                f"{stage} · Supervisor recovery attempt {attempt}",
                "supervisor",
            )
            add_message(
                task_id,
                "supervisor",
                f"RECOVERY: {stage} has not succeeded yet. Retrying with an adjusted approach "
                f"(attempt {attempt}).",
            )
            recovery_notes += (
                f"\n\nRECOVERY CONTEXT: Attempt {attempt - 1} failed. "
                f"Previous error: {str(last_error)[:500]}. "
                "Reassess the approach, correct the issue, simplify or use an alternative method, "
                "preserve completed work, and continue toward a valid deliverable. "
                "Never claim an external action that was not actually performed."
            )
            recovery_notes = recovery_notes[-5000:]
            await asyncio.sleep(min(2 ** min(attempt - 1, 6), 60))
        try:
            return await ask_model(
                system_prompt + " Act as a self-repairing agent: adapt methods, work around transient "
                "obstacles, and continue toward the requested deliverable.",
                user_prompt + recovery_notes,
            )
        except Exception as exc:
            last_error = exc
            detail = f"{type(exc).__name__}: {exc}".lower()
            hard_blockers = (
                "not configured", "401", "403", "402", "invalid api key",
                "authentication failed", "insufficient_quota", "model_not_found",
                "no available channel", "permission denied",
            )
            if any(marker in detail for marker in hard_blockers):
                add_message(
                    task_id,
                    "supervisor",
                    f"RECOVERY_BLOCKED: {stage} requires an external credential, account permission, "
                    f"or provider configuration that this agent cannot create or bypass. Details: {str(exc)[:500]}",
                )
                raise
            attempt += 1


def report_progress(task_id: int, stage: str, agent: str = 'orchestrator') -> None:
    set_active_agent(task_id, agent)
    add_message(task_id, 'system', 'PROGRESS:' + stage)


async def ask_model(system_prompt: str, user_prompt: str) -> str:
    global _MODEL_LAST_REQUEST_AT
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
    async with _MODEL_REQUEST_SEMAPHORE:
        for attempt in range(MAX_MODEL_ATTEMPTS):
            try:
                async with _MODEL_REQUEST_LOCK:
                    now = time.monotonic()
                    wait_for_slot = MODEL_MIN_REQUEST_INTERVAL - (now - _MODEL_LAST_REQUEST_AT)
                    if wait_for_slot > 0:
                        await asyncio.sleep(wait_for_slot)
                    _MODEL_LAST_REQUEST_AT = time.monotonic()
                async with httpx.AsyncClient(timeout=httpx.Timeout(MODEL_REQUEST_TIMEOUT, connect=10)) as client:
                    response = await client.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload)
                if response.is_error:
                    if response.status_code in (408, 425, 429) or response.status_code >= 500:
                        response.raise_for_status()
                    detail = response.text[:500].replace("\\n", " ")
                    raise RuntimeError(f"AI provider HTTP {response.status_code}: {detail}")
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
                retry_after = (
                    _retry_after_seconds(exc.response)
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429
                    else None
                )
                delay = retry_after if retry_after is not None else min(2 ** attempt, 30)
                await asyncio.sleep(min(delay + random.uniform(0.1, 0.8), 90))
    raise RuntimeError(f"AI provider request failed after {MAX_MODEL_ATTEMPTS} attempts: {type(last_error).__name__}: {last_error}")



async def _auto_capability_context(goal: str) -> str:
    """Route explicit/current-information requests to web search; media generation is paused."""
    text = goal.lower()
    needs_search = bool(re.search(
        r"latest|current|today|recent|news|price|weather|research|look up|search the web|খবর|আজকের|সর্বশেষ|বর্তমান|দাম|খুঁজে|তথ্য বের|ওয়েবে সার্চ|ওয়েব সার্চ",
        text,
    ))
    if not needs_search:
        return ""
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        return "Web search is needed for this request, but TAVILY_API_KEY is not configured."
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                os.getenv("TAVILY_API_URL", "https://api.tavily.com/search"),
                json={"api_key": key, "query": goal[:1000], "max_results": 5, "search_depth": "advanced"},
            )
        response.raise_for_status()
        rows = response.json().get("results", [])
        return ("AUTOMATIC LIVE WEB SEARCH RESULTS:\\n" +
            "\\n\\n".join(f"- {x.get('title', 'Source')}\\n  URL: {x.get('url', '')}\\n  Extract: {x.get('content', '')[:1800]}" for x in rows)
            if rows else "Automatic web search returned no results.")
    except Exception as exc:
        return f"Automatic web search failed ({type(exc).__name__}); do not claim live research succeeded."


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


def _apk_build_requested(goal: str) -> bool:
    text = goal.lower()
    return bool(
        re.search(r"\b(?:build|compile|generate|make)\b.{0,50}\bapk\b|\bapk\b.{0,50}\b(?:build|compile|generate|make)\b", text)
        or re.search(r"এপিকে.{0,50}(?:বানাও|তৈরি|বিল্ড)|(?:বানাও|তৈরি|বিল্ড).{0,50}এপিকে", text)
    )


def _github_repository_from_text(goal: str) -> str | None:
    url_match = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", goal, re.IGNORECASE)
    if url_match:
        return f"{url_match.group(1)}/{url_match.group(2).removesuffix('.git')}"
    explicit = re.search(r"(?:repo(?:sitory)?\s*[:=]?\s*)([A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100})", goal, re.IGNORECASE)
    if explicit:
        return explicit.group(1)
    return None


def _requested_workflow(goal: str) -> str | None:
    path = re.search(r"(?:\.github/workflows/)?([A-Za-z0-9_.-]+\.ya?ml)", goal, re.IGNORECASE)
    return path.group(1) if path else None


def _requested_ref(goal: str) -> str:
    match = re.search(r"(?:branch|ref)\s*[:=]?\s*([A-Za-z0-9_./-]+)", goal, re.IGNORECASE)
    return match.group(1) if match else "main"


async def _run_github_apk_build(task_id: int, goal: str) -> dict | None:
    """Run a GitHub Actions Android build only when the user explicitly requests an APK build."""
    if not _apk_build_requested(goal):
        return None
    repository = _github_repository_from_text(goal)
    if not repository:
        # Produce the source ZIP first; an APK workflow can be connected afterwards.
        return None
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "GitHub plugin is not connected. Open Plugins → GitHub, add GITHUB_TOKEN in Render Environment, "
            "then retry this task. Required permissions: Contents read, Actions read/write."
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("GitHub repository must be in owner/repository format.")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.get(f"https://api.github.com/repos/{repository}/actions/workflows", headers=headers, params={"per_page": 100})
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub could not list workflows ({response.status_code}): {response.text[:300]}")
        workflows = response.json().get("workflows", [])
        requested = _requested_workflow(goal)
        if requested:
            candidates = [w for w in workflows if w.get("path", "").endswith(requested) or w.get("name", "").lower() == requested.lower()]
        else:
            candidates = [
                w for w in workflows
                if w.get("state") == "active"
                and re.search(r"android|apk|build", (w.get("name", "") + " " + w.get("path", "")), re.IGNORECASE)
            ]
        if len(candidates) != 1:
            choices = "\n".join(f"- {w.get('name')} ({w.get('path')})" for w in workflows[:20])
            if not workflows:
                choices = "এই repository-তে কোনো GitHub Actions workflow পাওয়া যায়নি।"
            elif not candidates:
                choices = "Android/APK build নামে workflow খুঁজে পাওয়া যায়নি। Available workflows:\n" + choices
            else:
                choices = "একাধিক সম্ভাব্য workflow পাওয়া গেছে:\n" + "\n".join(f"- {w.get('name')} ({w.get('path')})" for w in candidates)
            question = "কোন GitHub Actions workflow দিয়ে APK build করব? .github/workflows/ এর workflow ফাইলের নাম লিখো।\n" + choices
            pause_task_for_input(task_id)
            add_message(task_id, "orchestrator", "CLARIFICATION_NEEDED:\n" + question)
            return {"task_id": task_id, "status": "needs_input", "questions": question}
        workflow = candidates[0]
        ref = _requested_ref(goal)
        dispatch = await client.post(
            f"https://api.github.com/repos/{repository}/actions/workflows/{workflow['id']}/dispatches",
            headers=headers,
            json={"ref": ref, "inputs": {}},
        )
        if dispatch.status_code >= 400:
            try:
                detail = dispatch.json().get("message", dispatch.text[:300])
            except ValueError:
                detail = dispatch.text[:300]
            raise RuntimeError(f"GitHub rejected the build request: {detail}")
    result = (
        f"GitHub Actions build request accepted.\n"
        f"Repository: {repository}\n"
        f"Workflow: {workflow.get('name')} ({workflow.get('path')})\n"
        f"Branch: {ref}\n"
        f"Track the build: https://github.com/{repository}/actions"
    )
    add_message(task_id, "orchestrator", result)
    finish_task(task_id, "completed", result)
    return {"task_id": task_id, "status": "completed", "result": result}


async def run_task(task_id: int, goal: str, skip_clarification: bool = False) -> dict:
    try:
        report_progress(task_id, "Task received · checking requested work", "orchestrator")
        github_result = await _run_github_apk_build(task_id, goal)
        if github_result is not None:
            return github_result
        if not skip_clarification:
            report_progress(task_id, "Checking whether more details are needed", "orchestrator")
            try:
                check = await asyncio.wait_for(clarification_check(goal), timeout=MODEL_REQUEST_TIMEOUT + 5)
            except Exception as exc:
                report_progress(task_id, "Clarification check failed · continuing with safe assumptions", "supervisor")
                add_message(task_id, "supervisor", f"RECOVERY: Clarification preflight failed; continuing without blocking the task. {type(exc).__name__}: {str(exc)[:400]}")
                check = {"needs_clarification": False, "questions": ""}
            if check["needs_clarification"] and check["questions"]:
                pause_task_for_input(task_id)
                add_message(task_id, "orchestrator", "CLARIFICATION_NEEDED:\\n" + check["questions"])
                return {"task_id": task_id, "status": "needs_input", "questions": check["questions"]}

        report_progress(task_id, "Preparing tools and context", "orchestrator")
        tool_context = await asyncio.wait_for(_auto_capability_context(goal), timeout=30)
        if tool_context:
            add_message(task_id, "tools", tool_context)
        web_context = tool_context

        specialist_keys = choose_agents(goal)
        software_request = bool(re.search(
            r"\b(app|application|website|web app|software|android|apk|mobile app|frontend|backend)\b|অ্যাপ|ওয়েবসাইট|সফটওয়্যার|মোবাইল অ্যাপ|ওয়েব অ্যাপ",
            goal, re.IGNORECASE,
        ))
        report_progress(task_id, "Supervisor is assigning agents and dependencies", "supervisor")
        supervisor_plan = build_supervisor_plan(goal, specialist_keys)
        if not validate_plan(supervisor_plan):
            raise RuntimeError("Supervisor generated an invalid task dependency graph.")

        # Execute the real, read-only Render preflight when its adapter is configured.
        # A deployment is a separate side effect and pauses until the user explicitly approves it.
        if "render" in supervisor_plan.get("integrations", []):
            render_result = await inspect_render_service()
            render_step = next((step for step in supervisor_plan["steps"] if step["id"] == "render_preflight"), None)
            if render_step:
                render_step["execution_state"] = render_result.get("state", "blocked")
            add_message(task_id, "render_agent",
                "SUPERVISOR_STEP_RESULT:render_preflight:" + json.dumps(render_result, ensure_ascii=False))
            if render_result.get("state") == "completed":
                supervisor_plan["execution_mode"] = "adapter_execution"
                tool_context += "\\n\\nLIVE RENDER PREFLIGHT (verified from Render API):\\n" + json.dumps(render_result, ensure_ascii=False)
            else:
                supervisor_plan["execution_mode"] = "partially_blocked"
                tool_context += "\\n\\nRENDER PREFLIGHT BLOCKED: " + str(render_result.get("reason", "Render adapter unavailable."))

            if render_deploy_requested(goal):
                github_changes_unexecuted = any(step["id"] == "code_work" for step in supervisor_plan["steps"])
                deploy_step = next((step for step in supervisor_plan["steps"] if step["id"] == "render_deploy"), None)
                if github_changes_unexecuted:
                    if deploy_step:
                        deploy_step["execution_state"] = "blocked"
                    tool_context += "\\nRender deployment blocked: the requested GitHub code-change/merge steps do not have an active execution adapter yet. No unverified revision will be deployed."
                    add_message(task_id, "render_agent",
                        "SUPERVISOR_STEP_RESULT:render_deploy:" + json.dumps({
                            "state": "blocked",
                            "reason": "GitHub code changes and merge are not executed by the current adapter set.",
                        }, ensure_ascii=False))
                else:
                    messages = task_messages(task_id)
                    approval_pending = any(
                        item["agent"] == "supervisor" and str(item["content"]).startswith("APPROVAL_REQUIRED:render_deploy")
                        for item in messages
                    )
                    latest_user = next((str(item["content"]).strip() for item in reversed(messages) if item["agent"] == "user"), "")
                    approved = approval_pending and latest_user.upper() in {"APPROVE", "APPROVED", "YES, DEPLOY"}
                    declined = approval_pending and latest_user.upper() in {"NO", "DECLINE", "CANCEL"}

                    if render_result.get("state") != "completed":
                        if deploy_step:
                            deploy_step["execution_state"] = "blocked"
                        tool_context += "\\nDeployment was not attempted because Render preflight or credentials were unavailable."
                    elif approved:
                        deploy_result = await deploy_render_service()
                        if deploy_step:
                            deploy_step["execution_state"] = deploy_result.get("state", "failed")
                        add_message(task_id, "render_agent",
                            "SUPERVISOR_STEP_RESULT:render_deploy:" + json.dumps(deploy_result, ensure_ascii=False))
                        tool_context += "\\n\\nRENDER DEPLOY ACTION RESULT:\\n" + json.dumps(deploy_result, ensure_ascii=False)
                    elif declined:
                        if deploy_step:
                            deploy_step["execution_state"] = "declined"
                        tool_context += "\\nUser declined the Render deployment. No deployment was triggered."
                    else:
                        if deploy_step:
                            deploy_step["execution_state"] = "awaiting_approval"
                        if not approval_pending:
                            add_message(task_id, "supervisor",
                                "APPROVAL_REQUIRED:render_deploy: Render preflight is complete. A deployment will change the live service. Type APPROVE to trigger it, or NO to continue without deploying.")
                        question = (
                            "Render service preflight is complete. The next step will trigger a live deployment. "
                            "Type exactly APPROVE to deploy, or NO to skip deployment."
                        )
                        add_message(task_id, "orchestrator", "CLARIFICATION_NEEDED:\\n" + question)
                        add_message(task_id, "supervisor", "SUPERVISOR_PLAN:" + json.dumps(supervisor_plan, ensure_ascii=False))
                        pause_task_for_input(task_id)
                        return {"task_id": task_id, "status": "needs_input", "questions": question}

        add_message(task_id, "supervisor", "SUPERVISOR_PLAN:" + json.dumps(supervisor_plan, ensure_ascii=False))
        add_message(task_id, "system", "AGENT_COMPLETED:supervisor")
        report_progress(task_id, "Orchestrator is preparing the execution plan", "orchestrator")
        planner = await ask_model_with_recovery(
            task_id, "orchestrator", "Orchestrator planning",
            "You are the Main Orchestrator working under the Supervisor Agent. "
            "Return a concise execution plan as JSON with summary, steps, and specialists. "
            "Respect the supplied dependency graph and approval gates. "
            "Delegate only text work unless a real integration adapter is explicitly available. "
            "Never claim external actions were executed.",
            "User goal:\n" + goal + "\n\nAutomatically selected tool results:\n" + web_context + "\n\nSupervisor dependency plan:\n" +
            json.dumps(supervisor_plan, ensure_ascii=False),
        )
        add_message(task_id, "orchestrator", planner)
        add_message(task_id, "system", "AGENT_COMPLETED:orchestrator")

        # Publish the selected team before execution so the dashboard can show
        # every assigned specialist as IN QUEUE until it actually starts.
        planned_agents = ["supervisor", "orchestrator"] + (["github_agent"] if "github" in supervisor_plan["integrations"] else []) + (["render_agent"] if "render" in supervisor_plan["integrations"] else []) + (["integration_agent"] if "api_plugin" in supervisor_plan["integrations"] else [])
        add_message(task_id, "system", "AGENTS_SELECTED:" + ",".join(planned_agents + specialist_keys + (["project_builder"] if software_request else []) + ["reviewer"]))
        active = []
        set_active_agents(task_id, active)
        active_lock = asyncio.Lock()

        async def run_specialist(key: str):
            agent = AGENTS[key]
            add_message(task_id, "system", "PROGRESS:Starting " + agent.name)
            async with active_lock:
                active.append(key)
                set_active_agents(task_id, active)
            prompt = (
                f"User goal:\n{goal}\n\nAutomatically selected tool results:\n{tool_context}\n\nOrchestrator plan:\n{planner}\n\n"
                f"Collaborating team: {', '.join(AGENTS[k].name for k in specialist_keys)} plus QA Reviewer. "
                "Produce a useful specialist contribution that can be shared with the other agents. "
                "Do not claim external actions."
            )
            try:
                output = await ask_model_with_recovery(task_id, key, agent.name + " contribution", 
                    f"You are {agent.name}, specialist in {agent.role}. {agent.instruction} "
                    "You are text-only: do not execute external actions, access accounts, or claim to have done so.",
                    prompt,
                )
                add_message(task_id, key, output)
                add_message(task_id, "system", "AGENT_COMPLETED:" + key)
                return {"key": key, "output": output, "error": None}
            except Exception as exc:
                note = f"{agent.name} could not complete its part after automatic retries: {type(exc).__name__}: {exc}"
                add_message(task_id, key, note)
                add_message(task_id, "system", "AGENT_FAILED:" + key)
                return {"key": key, "output": None, "error": note}
            finally:
                async with active_lock:
                    if key in active:
                        active.remove(key)
                    set_active_agents(task_id, active)

        # Execute specialists in Supervisor queue order. Only one specialist is active
        # at a time; all remaining selected specialists stay visibly IN QUEUE.
        results = []
        for key in specialist_keys:
            results.append(await run_specialist(key))
        successful = [item for item in results if item["output"]]
        if not successful:
            report_progress(task_id, "All specialists encountered errors · Supervisor is attempting team recovery", "supervisor")
            failure_context = "\n\n".join(item["error"] or "No usable output" for item in results)
            fallback = await ask_model_with_recovery(
                task_id, "recovery_agent", "Fallback team recovery",
                "You are the emergency recovery specialist. The original specialist team failed. "
                "Independently complete the user's request as far as possible, using a simpler alternative "
                "when needed. Be explicit about anything genuinely blocked and never invent external actions.",
                f"Original goal:\n{goal}\n\nSpecialist failures:\n{failure_context}\n\n"
                f"Available context:\n{tool_context}\n\nOrchestrator plan:\n{planner}",
                max_recoveries=3,
            )
            recovery_result = {"key": "recovery_agent", "output": fallback, "error": None}
            successful = [recovery_result]
            results.append(recovery_result)

        # Shared handoff gives each specialist's findings to the team coordinator and QA reviewer.
        team_context = "\n\n".join(
            f"### {AGENTS[item['key']].name if item['key'] in AGENTS else 'Supervisor Recovery Agent'}\n{item['output']}" for item in successful
        )
        # Supervisor remains accountable after delegation: inspect every specialist outcome.
        report_progress(task_id, "Supervisor is monitoring agent results and checking for gaps", "supervisor")
        outcome_context = "\n\n".join(
            f"### {AGENTS[item['key']].name if item['key'] in AGENTS else 'Supervisor Recovery Agent'} ({'SUCCESS' if item['output'] else 'FAILED'})\n"
            f"{item['output'] or item['error']}"
            for item in results
        )
        try:
            handoff = await ask_model_with_recovery(task_id, "supervisor", "Supervisor oversight", 
                "You are the Supervisor Agent and accountable team lead. Review the original goal, "
                "the dependency plan, and every specialist outcome. Identify missing requirements, "
                "contradictions, weak assumptions, failed contributions, and checks QA must perform. "
                "Give the QA Reviewer concrete verification steps and state unresolved gaps. "
                "Do not claim external actions occurred or invent missing results.",
                f"Original goal:\n{goal}\n\nDependency plan:\n"
                f"{json.dumps(supervisor_plan, ensure_ascii=False)}\n\n"
                f"Orchestrator plan:\n{planner}\n\nSpecialist outcomes:\n{outcome_context}",
            )
            add_message(task_id, "supervisor", "SUPERVISOR_OVERSIGHT:\n" + handoff)
            add_message(task_id, "system", "AGENT_COMPLETED:supervisor")
        except Exception as exc:
            handoff = (
                "Supervisor oversight model call failed after retries: "
                f"{type(exc).__name__}: {str(exc)[:500]}. QA must independently inspect all specialist outcomes."
            )
            add_message(task_id, "system", handoff)

        report_progress(task_id, "QA Reviewer is checking the combined result", "reviewer")
        final = await ask_model_with_recovery(task_id, "reviewer", "Final QA review", 
            "You are the QA Reviewer and final response editor. Follow the Supervisor oversight brief, verify its concerns against specialist work, and combine everything into one accurate, useful answer. "
            "Use the cross-agent handoff to resolve dependencies and contradictions. Correct unsupported claims, "
            "flag uncertainty, and never claim an external action was completed.",
            f"Original request:\n{goal}\n\nAutomatically selected tool results:\n{tool_context}\n\nSpecialist work:\n{team_context}\n\n"
            f"Supervisor oversight and QA instructions:\n{handoff or 'No supervisor oversight brief was available.'}",
        )
        add_message(task_id, "reviewer", final)
        add_message(task_id, "system", "AGENT_COMPLETED:reviewer")

        # Generate real downloadable source bundles for app and website requests.
        if software_request:
            report_progress(task_id, "Project Builder is generating actual source files", "project_builder")
            try:
                manifest_raw = await ask_model_with_recovery(
                    task_id, "project_builder", "Project source generation",
                    "You are a senior software engineer generating a complete, coherent starter project. "
                    "Return ONLY valid JSON: {\"name\":\"project name\",\"files\":[{\"path\":\"relative/path\",\"content\":\"complete file content\"}]}. "
                    "Create real, runnable source files for the requested platform, not a plan or pseudocode. "
                    "Include essential configuration, entry points, and a README with exact run/build steps. "
                    "Keep the project concise and within 30 files and 500 KB total source. "
                    "Never include credentials, API keys, .env files, private data, or generated binaries. "
                    "Do not claim that an APK was compiled, tests were run, or a website was deployed. "
                    "For Android, provide the Gradle/Kotlin project source; for a website, provide runnable source files.",
                    f"Original approved user requirements:\n{goal}\n\nSupervisor plan:\n"
                    f"{json.dumps(supervisor_plan, ensure_ascii=False)}\n\nSpecialist work:\n{team_context}\n\n"
                    f"QA review:\n{final}\n\nCreate the actual project source files now.",
                )
                archive_bytes, bundle_info = create_project_zip(manifest_raw)
                artifact_id = save_task_artifact(task_id, f"project-task-{task_id}.zip", "application/zip", archive_bytes)
                final += (
                    f"\n\nPROJECT FILES CREATED: {bundle_info['name']} · {bundle_info['files']} source files · "
                    f"{len(archive_bytes)} ZIP bytes. Download artifact ID: {artifact_id}. "
                    "This is source code, not a compiled APK."
                )
                add_message(task_id, "project_builder",
                    f"PROJECT_ARTIFACT_CREATED:{artifact_id}:{bundle_info['files']}:{bundle_info['name']}")
                add_message(task_id, "system", "AGENT_COMPLETED:project_builder")
            except Exception as artifact_exc:
                artifact_note = (
                    "\n\nPROJECT FILE GENERATION FAILED: "
                    f"{type(artifact_exc).__name__}: {str(artifact_exc)[:500]}. "
                    "The QA result is available, but no downloadable source ZIP was created."
                )
                final += artifact_note
                add_message(task_id, "project_builder", artifact_note)
                add_message(task_id, "system", "AGENT_FAILED:project_builder")

        finish_task(task_id, "completed", final)
        return {
            "task_id": task_id,
            "status": "completed",
            "result": final,
            "agents": ["supervisor", "orchestrator"] + specialist_keys + ["reviewer"],
        }
    except Exception as exc:
        message = f"Task failed: {type(exc).__name__}: {str(exc)[:1200]}"
        try:
            add_message(task_id, "system", message)
            finish_task(task_id, "failed", message)
        except Exception:
            pass
        return {"task_id": task_id, "status": "failed", "error": message}
