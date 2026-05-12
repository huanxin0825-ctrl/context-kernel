from __future__ import annotations

from typing import Any

from .context import ContextBuilder
from .policy import assess_request_policy
from .storage import Workspace
from .tokenizer import estimate_tokens


CODE_TERMS = {
    "code",
    "cli",
    "debug",
    "edit",
    "file",
    "fix",
    "implement",
    "patch",
    "refactor",
    "test",
}
RESEARCH_TERMS = {"compare", "current", "latest", "news", "price", "research", "today", "verify"}


class ExecutionPlanner:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def plan(
        self,
        request: str,
        total_budget: int | None,
        profile: str = "balanced",
        task_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        comparison = ContextBuilder(self.workspace).compare(
            request,
            total_budget,
            profile,
            task_id=task_id,
            resume=resume,
        )
        packet = comparison["kernel"]["packet"]
        route = classify_route(request, packet)
        policy = assess_request_policy(self.workspace, request)
        warnings = list(packet.get("omissions", []))
        if packet["budget"]["over_budget"]:
            warnings.append("Do not execute provider call until context is reduced or budget is raised.")
        warnings.extend(policy["warnings"])

        return {
            "request": request,
            "profile": comparison["profile"],
            "route": route,
            "task": summarize_task(packet),
            "policy": policy,
            "budget": packet["budget"],
            "selection": {
                "memory": summarize_memory(packet),
                "skills": summarize_skills(packet),
            },
            "savings": comparison["savings"],
            "actions": planned_actions(packet),
            "warnings": warnings,
        }


def classify_route(request: str, packet: dict[str, Any]) -> dict[str, Any]:
    request_terms = {term.strip(".,:;!?()[]{}\"'").casefold() for term in request.split()}
    has_code = bool(request_terms.intersection(CODE_TERMS))
    has_research = bool(request_terms.intersection(RESEARCH_TERMS))
    selected_items = len(packet.get("memory", [])) + len(packet.get("skills", []))
    request_tokens = estimate_tokens(request)

    if has_research:
        mode = "research_or_verification"
    elif has_code:
        mode = "code_or_file_work"
    else:
        mode = "direct_answer"

    if request_tokens > 180 or selected_items >= 5:
        complexity = "high"
    elif request_tokens > 80 or selected_items >= 3:
        complexity = "medium"
    else:
        complexity = "low"

    return {
        "mode": mode,
        "complexity": complexity,
        "reason": route_reason(mode, complexity, selected_items),
    }


def summarize_memory(packet: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["record"]["id"],
            "kind": item["record"]["kind"],
            "score": item["score"],
            "reason": item["reason"],
            "estimated_tokens": item["estimated_tokens"],
        }
        for item in packet.get("memory", [])
    ]


def summarize_skills(packet: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["contract"]["id"],
            "name": item["contract"]["name"],
            "level": item["level"],
            "score": item["score"],
            "reason": item["reason"],
            "estimated_tokens": item["estimated_tokens"],
        }
        for item in packet.get("skills", [])
    ]


def summarize_task(packet: dict[str, Any]) -> dict[str, Any]:
    task = packet.get("task", {})
    brief = task.get("brief")
    if not brief:
        return {"resume": False}
    return {
        "resume": True,
        "id": brief["task"]["id"],
        "title": brief["task"]["title"],
        "status": brief["task"]["status"],
        "estimated_tokens": brief.get("estimated_tokens", 0),
        "plan": brief.get("plan"),
    }


def planned_actions(packet: dict[str, Any]) -> list[str]:
    actions = [
        "Assemble the minimal context packet shown in this plan.",
        "Call the selected provider only after budget checks pass.",
        "Write a trace with selected memory, selected skills, provider usage, and verifier checks.",
    ]
    if packet["budget"]["over_budget"]:
        return [
            "Stop before provider execution because the packet is over budget.",
            "Reduce selected memory or skill levels, or choose a larger profile.",
        ]
    return actions


def route_reason(mode: str, complexity: str, selected_items: int) -> str:
    return f"mode={mode}; complexity={complexity}; selected_context_items={selected_items}"
