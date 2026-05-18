from __future__ import annotations

from typing import Any


def summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": plan["route"],
        "budget": plan["budget"],
        "task": plan["task"],
        "selection": {
            "memory_count": len(plan["selection"]["memory"]),
            "skill_count": len(plan["selection"]["skills"]),
        },
        "warnings": plan["warnings"],
    }


def agent_step_result(
    *,
    index: int,
    status: str,
    can_continue: bool,
    plan: dict[str, Any],
    stop_reason: str = "",
    reason: str | None = None,
    diagnostic: dict[str, Any] | None = None,
    trace_id: str | None = None,
    model_role: str | None = None,
    model: str | None = None,
    routing_reason: str | None = None,
    aux_review: dict[str, Any] | None = None,
    tool_trace_id: str | None = None,
    action: dict[str, Any] | None = None,
    tool: dict[str, Any] | None = None,
    recovery_tools: list[dict[str, Any]] | None = None,
    tokens: dict[str, Any] | None = None,
    verifier_ok: bool = False,
    contract_recovered: bool | None = None,
    final_response: str | None = None,
    materialized_files: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "index": index,
        "status": status,
        "continue": can_continue,
        "stop_reason": stop_reason,
        "diagnostic": diagnostic,
        "plan": summarize_plan(plan),
        "trace_id": trace_id,
        "tool_trace_id": tool_trace_id,
        "action": action,
        "tokens": tokens or {},
        "verifier_ok": verifier_ok,
    }
    if reason is not None:
        result["reason"] = reason
    if model_role is not None:
        result["model_role"] = model_role
    if model is not None:
        result["model"] = model
    if routing_reason is not None:
        result["routing_reason"] = routing_reason
    if aux_review is not None:
        result["aux_review"] = aux_review
    if contract_recovered is not None:
        result["contract_recovered"] = contract_recovered
    if tool is not None:
        result["tool"] = tool
    if recovery_tools is not None:
        result["recovery_tools"] = recovery_tools
    if final_response is not None:
        result["final_response"] = final_response
    if materialized_files is not None:
        result["materialized_files"] = materialized_files
    return result
