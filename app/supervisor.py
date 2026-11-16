"""Supervisor planning and safety policy for multi-agent tasks."""
from __future__ import annotations

import re
from typing import Any


TOOL_CAPABILITIES = {
    "github": {"read", "branch_write", "file_write", "workflow_dispatch", "pull_request"},
    "render": {"service_read", "logs_read", "deploy", "environment_write"},
    "api_plugin": {"api_read", "api_write"},
    "agents": {"research", "coding", "writing", "data", "qa"},
}

APPROVAL_REQUIRED = {
    "branch_write", "file_write", "workflow_dispatch", "pull_request",
    "merge", "deploy", "environment_write", "api_write", "delete",
}


def detect_integrations(goal: str) -> set[str]:
    text = goal.lower()
    found: set[str] = set()
    if re.search(r"github|git hub|repository|repo\b|pull request|\bpr\b|commit|branch|apk build", text):
        found.add("github")
    if re.search(r"render|deploy|deployment|hosting|server|রেন্ডার|ডিপ্লয়|ডেপ্লয়", text):
        found.add("render")
    if re.search(r"plugin|api integration|connect .*service|external api", text):
        found.add("api_plugin")
    return found


def render_deploy_requested(goal: str) -> bool:
    """Avoid treating a request to inspect deploy health as permission to deploy."""
    text = goal.lower()
    return bool(re.search(
        r"\b(?:deploy|redeploy|release)\s+(?:to|on|into)\s+(?:render|production|the\s+service|the\s+app|the\s+site)\b|"
        r"\b(?:deploy|redeploy)\s+(?:the\s+)?(?:app|site|service|it|this)\b|"
        r"\b(?:render|app|site|service)\s+(?:now|again|please)\s+(?:deploy|redeploy)\b|"
        r"ডিপ্লয়\s*(কর|দাও|চালু)|ডেপ্লয়\s*(কর|দাও|চালু)",
        text,
    ))


def github_write_requested(goal: str) -> bool:
    text = goal.lower()
    return bool(re.search(
        r"\b(fix|update|change|edit|modify|add|remove|delete|commit|push|create|write)\b|"
        r"ঠিক\s*কর|পরিবর্তন\s*কর|যোগ\s*কর|মুছে\s*দাও",
        text,
    ))


def build_supervisor_plan(goal: str, specialists: list[str] | None = None) -> dict[str, Any]:
    """Return an inspectable dependency graph with approval gates only for requested writes."""
    integrations = detect_integrations(goal)
    specialists = list(dict.fromkeys(specialists or ["research", "writer"]))
    steps: list[dict[str, Any]] = []

    def add(step_id: str, title: str, agent: str, tool: str = "agents",
            capability: str = "research", depends_on: list[str] | None = None,
            approval: bool = False, note: str = "") -> None:
        steps.append({
            "id": step_id,
            "title": title,
            "agent": agent,
            "tool": tool,
            "capability": capability,
            "depends_on": depends_on or [],
            "approval_required": approval or capability in APPROVAL_REQUIRED,
            "execution_state": "planned",
            "note": note,
        })

    if "github" in integrations:
        add("github_inspect", "Inspect repository, workflows and current state",
            "github_agent", "github", "read")
        if github_write_requested(goal):
            add("code_work", "Implement requested repository changes",
                "coding_agent", "github", "file_write", ["github_inspect"], True,
                "Requires a connected GitHub credential and scoped write permission.")
            add("quality_checks", "Run tests and inspect the proposed diff",
                "qa_agent", "agents", "qa", ["code_work"])
            add("pull_request", "Prepare a pull request for review",
                "github_agent", "github", "pull_request", ["quality_checks"], True)
            add("merge", "Merge only after explicit approval",
                "supervisor", "github", "merge", ["pull_request"], True)

    if "render" in integrations:
        dependency = ["merge"] if "merge" in {step["id"] for step in steps} else []
        add("render_preflight", "Check Render service, latest commit and deploy health",
            "render_agent", "render", "service_read", dependency)
        if render_deploy_requested(goal):
            add("render_deploy", "Deploy the verified GitHub revision",
                "render_agent", "render", "deploy", ["render_preflight"], True,
                "Requires a configured Render API adapter and a fresh explicit approval.")

    if "api_plugin" in integrations:
        add("plugin_discovery", "Check configured API plugin capabilities",
            "integration_agent", "api_plugin", "api_read")
        if re.search(r"\b(create|update|change|delete|send|submit|modify)\b", goal, re.IGNORECASE):
            add("plugin_write", "Perform approved external API changes",
                "integration_agent", "api_plugin", "api_write", ["plugin_discovery"], True)

    if not integrations:
        for index, key in enumerate(specialists, start=1):
            add(f"specialist_{index}", f"{key.title()} specialist contribution",
                key, "agents", key if key in {"research", "coding", "writing", "data", "qa"} else "research")
        add("final_review", "Review and consolidate team output", "reviewer",
            "agents", "qa", [f"specialist_{i}" for i in range(1, len(specialists) + 1)])

    return {
        "supervisor": "Supervisor Agent",
        "goal": goal[:1200],
        "integrations": sorted(integrations),
        "execution_mode": "adapter_execution_pending",
        "steps": steps,
        "policy": {
            "read_only_steps_may_run_without_side_effect_approval": True,
            "mutating_steps_require_explicit_approval": True,
            "unknown_tools_are_never_assumed_available": True,
        },
        "notice": (
            "Read-only actions run only when a matching adapter and credentials are configured. "
            "External changes require a fresh approval and are reported only after the provider confirms them."
        ),
    }


def validate_plan(plan: dict[str, Any]) -> bool:
    """Reject duplicate IDs, missing dependencies, and dependency cycles."""
    steps = plan.get("steps", [])
    ids = [step.get("id") for step in steps]
    if not ids or len(ids) != len(set(ids)) or any(not item for item in ids):
        return False
    known = set(ids)
    graph = {step["id"]: set(step.get("depends_on", [])) for step in steps}
    if any(not deps.issubset(known) for deps in graph.values()):
        return False
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return False
        if node in visited:
            return True
        visiting.add(node)
        for dependency in graph[node]:
            if not visit(dependency):
                return False
        visiting.remove(node)
        visited.add(node)
        return True

    return all(visit(node) for node in graph)
