from __future__ import annotations

from typing import Any, Callable

from .planner import ExecutionPlanner
from .storage import Workspace
from .loop_steps import agent_step_result


def prepare_agent_step_plan(
    workspace: Workspace,
    *,
    request: str,
    task_id: str,
    budget: int | None,
    profile: str,
    index: int,
    max_steps: int,
    allow_over_budget: bool,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> tuple[dict[str, Any], int | None, dict[str, Any] | None]:
    effective_budget = budget
    plan = ExecutionPlanner(workspace).plan(
        request,
        effective_budget,
        profile,
        task_id=task_id,
        resume=True,
    )
    if plan["budget"]["over_budget"] and budget is None:
        expanded_budget = adaptive_context_budget(plan)
        effective_budget = expanded_budget
        emit_plan_progress(
            progress_callback,
            {
                "event": "budget_expand",
                "step": index,
                "max_steps": max_steps,
                "estimated_used": plan["budget"]["estimated_used"],
                "old_budget": plan["budget"]["total"],
                "new_budget": expanded_budget,
            },
        )
        plan = ExecutionPlanner(workspace).plan(
            request,
            effective_budget,
            profile,
            task_id=task_id,
            resume=True,
        )
    if plan["budget"]["over_budget"] and not allow_over_budget:
        diagnostic = {
            "category": "context_budget",
            "message": f"Context packet is over budget: used {plan['budget']['estimated_used']} / {plan['budget']['total']} tokens.",
            "suggestion": "Use a leaner profile, run /compact, or pass a larger explicit --budget when you intentionally want a hard budget.",
        }
        return plan, effective_budget, agent_step_result(
            index=index,
            status="blocked",
            can_continue=False,
            plan=plan,
            stop_reason="Agent loop stopped: context packet is over budget.",
            reason="context packet is over budget",
            diagnostic=diagnostic,
            trace_id=None,
            tool_trace_id=None,
            action=None,
            tokens={},
            verifier_ok=False,
        )
    return plan, effective_budget, None


def adaptive_context_budget(plan: dict[str, Any]) -> int:
    used = int(plan.get("budget", {}).get("estimated_used", 0) or 0)
    current = int(plan.get("budget", {}).get("total", 0) or 0)
    cushion = max(800, int(used * 0.25))
    return min(32000, max(current * 2, used + cushion, 6000))


def emit_plan_progress(callback: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        return
