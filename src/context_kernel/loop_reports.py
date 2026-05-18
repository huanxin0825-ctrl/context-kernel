from __future__ import annotations

from typing import Any

from .loop_actions import compact
from .memory import MemoryStore
from .storage import Workspace
from .tasks import TaskStore


def write_agent_run_memory(workspace: Workspace, tasks: TaskStore, report: dict[str, Any]) -> dict[str, Any]:
    memory = MemoryStore(workspace)
    text = (
        f"Agent run {report['id']} for task {report['task_id']} completed with status={report['status']}; "
        f"steps={len(report.get('steps', []))}; total_tokens={report.get('totals', {}).get('total_tokens', 0)}; "
        f"request='{compact(report.get('request', ''), limit=160)}'; "
        f"outcome='{compact(report.get('final_response') or summarize_report_outcome(report), limit=240)}'."
    )
    record = memory.add(
        "task_state",
        text,
        [
            "auto",
            "agent",
            f"agent_run:{report['id']}",
            f"status:{report['status']}",
        ],
    )
    tasks.attach(report["task_id"], "memory", record.id)
    return {
        "enabled": True,
        "candidate_count": 1,
        "written_count": 1,
        "records": [record.to_dict()],
    }


def compact_saved_agent_report(report: dict[str, Any]) -> dict[str, Any]:
    steps = report.get("steps", [])
    return {
        "id": report["id"],
        "created_at": report.get("created_at"),
        "completed_at": report.get("completed_at"),
        "request": compact(str(report.get("request", "")), limit=500),
        "task_id": report.get("task_id"),
        "status": report.get("status"),
        "max_steps": report.get("max_steps"),
        "model_routing": report.get("model_routing"),
        "diagnostic": compact_saved_diagnostic(report.get("diagnostic")),
        "steps": [compact_saved_step(step) for step in steps],
        "final_response": compact(str(report.get("final_response") or ""), limit=800) or None,
        "materialized_files": list(report.get("materialized_files", [])),
        "state": compact_saved_state(report.get("state", {})),
        "totals": compact_saved_tokens(report.get("totals", {})),
        "storage": {
            "detail_level": "compact_v1",
            "step_count": len(steps),
            "full_details_in": {
                "run_traces": unique_non_empty(collect_run_trace_ids(steps)),
                "tool_traces": unique_non_empty(collect_tool_trace_ids(steps)),
            },
        },
    }


def compact_saved_step(step: dict[str, Any]) -> dict[str, Any]:
    saved = {
        "index": step.get("index"),
        "status": step.get("status"),
        "continue": bool(step.get("continue")),
        "trace_id": step.get("trace_id"),
        "tool_trace_id": step.get("tool_trace_id"),
        "model_role": step.get("model_role"),
        "model": step.get("model"),
        "routing_reason": compact(str(step.get("routing_reason", "")), limit=180) or None,
        "aux_review": compact_saved_aux_review(step.get("aux_review", {})),
        "action": step.get("action"),
        "tokens": compact_saved_tokens(step.get("tokens", {})),
        "verifier_ok": step.get("verifier_ok"),
        "contract_recovered": bool(step.get("contract_recovered")),
    }
    diagnostic = compact_saved_diagnostic(step.get("diagnostic"))
    if diagnostic:
        saved["diagnostic"] = diagnostic
    if step.get("stop_reason"):
        saved["stop_reason"] = compact(str(step.get("stop_reason", "")), limit=180)
    plan = step.get("plan")
    if isinstance(plan, dict):
        saved["plan"] = compact_saved_plan(plan)
    tool = step.get("tool")
    if isinstance(tool, dict) and tool:
        saved["tool"] = compact_saved_tool(tool)
    recovery_tools = step.get("recovery_tools")
    if isinstance(recovery_tools, list) and recovery_tools:
        saved["recovery_tools"] = [
            compact_saved_tool(item)
            for item in recovery_tools
            if isinstance(item, dict)
        ]
    if step.get("final_response"):
        saved["final_response"] = compact(str(step.get("final_response", "")), limit=320)
    if step.get("materialized_files"):
        saved["materialized_files"] = list(step.get("materialized_files", []))
    return saved


def compact_saved_diagnostic(diagnostic: Any) -> dict[str, str] | None:
    if not isinstance(diagnostic, dict) or not diagnostic:
        return None
    return {
        "category": compact(str(diagnostic.get("category", "")), limit=80),
        "message": compact(str(diagnostic.get("message", "")), limit=400),
        "suggestion": compact(str(diagnostic.get("suggestion", "")), limit=240),
    }


def compact_saved_aux_review(review: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(review, dict) or not review:
        return {"enabled": False}
    saved = {
        "enabled": bool(review.get("enabled")),
        "reason": compact(str(review.get("reason", "")), limit=180) or None,
    }
    if review.get("enabled"):
        saved.update(
            {
                "trace_id": review.get("trace_id"),
                "model": review.get("model"),
                "ok": bool(review.get("ok")),
                "risk": review.get("risk"),
                "recommendation": review.get("recommendation"),
                "notes": [compact(str(item), limit=160) for item in review.get("notes", [])[:5]],
                "tokens": compact_saved_tokens(review.get("tokens", {})),
                "verifier_ok": bool(review.get("verifier_ok")),
            }
        )
        diagnostic = compact_saved_diagnostic(review.get("diagnostic"))
        if diagnostic:
            saved["diagnostic"] = diagnostic
    return saved


def compact_saved_plan(plan: dict[str, Any]) -> dict[str, Any]:
    route = plan.get("route", {})
    budget = plan.get("budget", {})
    task = plan.get("task", {})
    warnings = plan.get("warnings", [])
    saved = {
        "route": {
            "mode": route.get("mode"),
            "complexity": route.get("complexity"),
        },
        "budget": {
            "profile": budget.get("profile"),
            "total": budget.get("total"),
            "estimated_used": budget.get("estimated_used"),
            "estimated_remaining": budget.get("estimated_remaining"),
            "over_budget": budget.get("over_budget"),
        },
        "selection": {
            "memory_count": plan.get("selection", {}).get("memory_count"),
            "skill_count": plan.get("selection", {}).get("skill_count"),
        },
        "task": {
            "id": task.get("id"),
            "status": task.get("status"),
            "resume": task.get("resume"),
            "estimated_tokens": task.get("estimated_tokens"),
        },
    }
    if route.get("reason"):
        saved["route"]["reason"] = compact(str(route.get("reason", "")), limit=180)
    if isinstance(warnings, list) and warnings:
        saved["warnings"] = [compact(str(item), limit=140) for item in warnings[:3]]
        saved["warning_count"] = len(warnings)
    return saved


def compact_saved_tool(tool: dict[str, Any]) -> dict[str, Any]:
    saved = {
        "id": tool.get("id"),
        "name": tool.get("name") or tool.get("tool"),
        "ok": tool.get("ok"),
        "blocked": tool.get("blocked"),
    }
    if tool.get("summary"):
        saved["summary"] = compact(str(tool.get("summary", "")), limit=240)
    if tool.get("error"):
        saved["error"] = compact(str(tool.get("error", "")), limit=240)
    return saved


def compact_saved_state(state: dict[str, Any]) -> dict[str, Any]:
    records = state.get("records", [])
    saved = {
        "enabled": bool(state.get("enabled")),
        "candidate_count": state.get("candidate_count", 0),
        "written_count": state.get("written_count", 0),
    }
    if isinstance(records, list) and records:
        saved["record_count"] = len(records)
        saved["records"] = [
            compact_saved_memory_record(record)
            for record in records[:3]
            if isinstance(record, dict)
        ]
    return saved


def compact_saved_memory_record(record: dict[str, Any]) -> dict[str, Any]:
    saved = {
        "id": record.get("id"),
        "kind": record.get("kind"),
        "created_at": record.get("created_at"),
        "tags": record.get("tags", []),
    }
    if record.get("text"):
        saved["text"] = compact(str(record.get("text", "")), limit=240)
    return saved


def compact_saved_tokens(tokens: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": int(tokens.get("input_tokens", 0) or 0),
        "output_tokens": int(tokens.get("output_tokens", 0) or 0),
        "total_tokens": int(tokens.get("total_tokens", 0) or 0),
    }


def render_agent_response(action: dict[str, Any]) -> str:
    return str(action.get("message", "")).strip()


def summarize_report_outcome(report: dict[str, Any]) -> str:
    steps = report.get("steps", [])
    if not steps:
        return "no steps were recorded"
    last = steps[-1]
    action = (last.get("action") or {}).get("action")
    tool = last.get("tool", {})
    if action == "respond":
        return str((last.get("action") or {}).get("message") or "final response produced")
    if tool:
        summary = tool.get("summary") or tool.get("error") or "tool step completed"
        return f"{tool.get('name')} -> {summary}"
    return f"last_step_status={last.get('status')}"


def add_tokens(total: dict[str, int], tokens: dict[str, int]) -> None:
    total["input_tokens"] += int(tokens.get("input_tokens", 0) or 0)
    total["output_tokens"] += int(tokens.get("output_tokens", 0) or 0)
    total["total_tokens"] += int(tokens.get("total_tokens", 0) or 0)


def collect_tool_trace_ids(steps: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for step in steps:
        if step.get("tool_trace_id"):
            ids.append(str(step.get("tool_trace_id")))
        for recovery in step.get("recovery_tools", []):
            if isinstance(recovery, dict) and recovery.get("id"):
                ids.append(str(recovery.get("id")))
    return ids


def collect_run_trace_ids(steps: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for step in steps:
        if step.get("trace_id"):
            ids.append(str(step.get("trace_id")))
        review = step.get("aux_review", {})
        if isinstance(review, dict) and review.get("trace_id"):
            ids.append(str(review.get("trace_id")))
    return ids


def unique_non_empty(values: list[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered
