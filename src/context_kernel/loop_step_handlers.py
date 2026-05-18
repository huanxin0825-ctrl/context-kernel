from __future__ import annotations

from typing import Any, Callable

from .loop_actions import (
    action_progress_label,
    action_progress_target,
    compact,
    execute_agent_action,
    summarize_action,
    summarize_tool_result,
)
from .loop_materialize import (
    extract_response_code_blocks,
    looks_like_code_artifact_request,
    materialize_code_response_if_needed,
)
from .loop_progress import emit_agent_progress
from .loop_recovery import auto_recovery_tools, diagnose_tool_result, final_tool_stop_reason
from .loop_reports import render_agent_response
from .loop_steps import agent_step_result
from .tasks import TaskStore
from .tools import ToolExecutor


def handle_respond_action(
    *,
    tasks: TaskStore,
    tools: ToolExecutor,
    request: str,
    task_id: str,
    index: int,
    max_steps: int,
    plan: dict[str, Any],
    trace: dict[str, Any],
    selected_role: str,
    selected_model: str | None,
    routing_reason: str,
    review: dict[str, Any],
    action: dict[str, Any],
    tokens: dict[str, int],
    verifier_ok: bool,
    contract_recovered: bool,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    response_text = render_agent_response(action)
    emit_agent_progress(
        progress_callback,
        {
            "event": "action_start",
            "step": index,
            "max_steps": max_steps,
            "action": "respond",
            "label": "preparing final response",
        },
    )
    if looks_like_code_artifact_request(request) and extract_response_code_blocks(response_text):
        emit_agent_progress(
            progress_callback,
            {
                "event": "materialize_start",
                "step": index,
                "max_steps": max_steps,
                "action": "write_file",
                "label": "saving generated code to workspace files",
            },
        )
    materialized = materialize_code_response_if_needed(tools, request, response_text)
    if materialized:
        for trace_result in materialized["traces"]:
            tasks.attach(task_id, "tool", trace_result["id"])
        tasks.step(
            task_id,
            f"Agent materialized code response into file(s): {', '.join(materialized['paths'])}",
            kind="agent_tool",
            refs={"run_traces": [trace["id"]], "tool_traces": [item["id"] for item in materialized["traces"]]},
        )
        response_text = materialized["message"]
        emit_agent_progress(
            progress_callback,
            {
                "event": "materialize_end",
                "step": index,
                "max_steps": max_steps,
                "action": "write_file",
                "ok": True,
                "paths": materialized["paths"],
            },
        )
    tasks.step(
        task_id,
        f"Agent response: {compact(response_text, limit=400)}",
        kind="agent_response",
        refs={"run_traces": [trace["id"]]},
    )
    return agent_step_result(
        index=index,
        status="responded",
        can_continue=False,
        plan=plan,
        stop_reason="Agent loop stopped: a final response was produced.",
        trace_id=trace["id"],
        model_role=selected_role,
        model=selected_model,
        routing_reason=routing_reason,
        aux_review=review,
        tool_trace_id=None,
        action=summarize_action(action),
        tokens=tokens,
        verifier_ok=verifier_ok,
        contract_recovered=contract_recovered,
        final_response=response_text,
        materialized_files=materialized["paths"] if materialized else [],
    )


def handle_tool_action(
    *,
    tasks: TaskStore,
    tools: ToolExecutor,
    request: str,
    task_id: str,
    index: int,
    max_steps: int,
    plan: dict[str, Any],
    trace: dict[str, Any],
    selected_role: str,
    selected_model: str | None,
    routing_reason: str,
    review: dict[str, Any],
    action: dict[str, Any],
    tokens: dict[str, int],
    verifier_ok: bool,
    contract_recovered: bool,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    emit_agent_progress(
        progress_callback,
        {
            "event": "action_start",
            "step": index,
            "max_steps": max_steps,
            "action": action["action"],
            "label": action_progress_label(action),
            "target": action_progress_target(action),
        },
    )
    tool_result = execute_agent_action(tools, action)
    tasks.attach(task_id, "tool", tool_result["id"])
    tool_summary = summarize_tool_result(tool_result)
    emit_agent_progress(
        progress_callback,
        {
            "event": "action_end",
            "step": index,
            "max_steps": max_steps,
            "action": action["action"],
            "ok": tool_result["ok"],
            "blocked": tool_result["blocked"],
            "trace_id": tool_result["id"],
            "summary": tool_summary,
        },
    )
    tasks.step(
        task_id,
        f"Agent step {index} executed {action['action']}: {tool_summary}",
        kind="agent_tool",
        refs={"run_traces": [trace["id"]], "tool_traces": [tool_result["id"]]},
    )
    if index < max_steps and not tool_result["blocked"] and not tool_result["ok"]:
        emit_agent_progress(
            progress_callback,
            {
                "event": "recovery_start",
                "step": index,
                "max_steps": max_steps,
                "action": action["action"],
                "label": "collecting recovery context",
            },
        )
    recovery_tools = auto_recovery_tools(tools, request, action, tool_result) if index < max_steps else []
    if recovery_tools:
        for recovery in recovery_tools:
            tasks.attach(task_id, "tool", recovery["id"])
        recovery_summary = "; ".join(
            f"{item['tool']}:{summarize_tool_result(item)}"
            for item in recovery_tools
        )
        tasks.step(
            task_id,
            f"Agent recovery prepared after {action['action']}: {recovery_summary}",
            kind="agent_recovery",
            refs={"tool_traces": [item["id"] for item in recovery_tools]},
        )
        emit_agent_progress(
            progress_callback,
            {
                "event": "recovery_end",
                "step": index,
                "max_steps": max_steps,
                "action": action["action"],
                "count": len(recovery_tools),
                "summary": recovery_summary,
            },
        )
    can_continue = index < max_steps and (tool_result["ok"] or bool(recovery_tools))
    if tool_result["blocked"]:
        status = "blocked"
    elif not tool_result["ok"]:
        status = "recovery_prepared" if can_continue else "needs_review"
    else:
        status = "ok" if can_continue else "stopped"
    diagnostic = diagnose_tool_result(tool_result) if not can_continue and not tool_result["ok"] else None
    return agent_step_result(
        index=index,
        status=status,
        can_continue=can_continue,
        plan=plan,
        stop_reason=final_tool_stop_reason(tool_result, recovery_tools=recovery_tools, max_steps=max_steps) if not can_continue else "",
        diagnostic=diagnostic,
        trace_id=trace["id"],
        model_role=selected_role,
        model=selected_model,
        routing_reason=routing_reason,
        aux_review=review,
        tool_trace_id=tool_result["id"],
        action=summarize_action(action),
        tool={
            "id": tool_result["id"],
            "name": tool_result["tool"],
            "ok": tool_result["ok"],
            "blocked": tool_result["blocked"],
            "error": tool_result.get("error"),
            "summary": tool_summary,
        },
        recovery_tools=[
            {
                "id": item["id"],
                "name": item["tool"],
                "ok": item["ok"],
                "blocked": item["blocked"],
                "summary": summarize_tool_result(item),
            }
            for item in recovery_tools
        ],
        tokens=tokens,
        verifier_ok=verifier_ok,
        contract_recovered=contract_recovered,
    )
