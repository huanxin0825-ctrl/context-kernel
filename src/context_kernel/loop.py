from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

from .models import utc_now
from .planner import ExecutionPlanner
from .providers import env_value, extract_anchor_patch_instruction, extract_patch_instruction, extract_write_instruction
from .runner import AgentRunner
from .storage import Workspace
from .tasks import TaskStore
from .tools import MAX_CAPTURE_CHARS, ToolExecutor
from .loop_actions import (
    ALLOWED_ACTIONS,
    TOOL_ACTIONS,
    action_progress_label,
    action_progress_target,
    compact,
    execute_agent_action,
    parse_agent_action,
    repeated_agent_action,
    summarize_action,
    summarize_tool_result,
)
from .loop_materialize import (
    extract_response_code_blocks,
    looks_like_code_artifact_request,
    materialize_code_response_if_needed,
)
from .loop_reports import (
    add_tokens,
    compact_saved_agent_report,
    render_agent_response,
    write_agent_run_memory,
)

DEFAULT_PRIMARY_MODEL = "gpt-5.5"
DEFAULT_AUXILIARY_MODEL = "gpt-5.3-codex"
MODEL_ROUTING_MODES = {"auto", "primary", "auxiliary"}
AUX_REVIEW_MODES = {"auto", "off", "always"}


def emit_agent_progress(callback: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        return


class AgentLoop:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.tasks = TaskStore(workspace)
        self.tools = ToolExecutor(workspace)

    def run(
        self,
        request: str,
        *,
        provider_name: str,
        budget: int | None,
        profile: str = "balanced",
        model: str | None = None,
        aux_model: str | None = None,
        model_routing: str = "primary",
        aux_review: str = "auto",
        base_url: str | None = None,
        task_id: str | None = None,
        max_steps: int = 5,
        remember: bool = True,
        allow_over_budget: bool = False,
        expect_json: bool = False,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if model_routing not in MODEL_ROUTING_MODES:
            raise ValueError(f"model_routing must be one of: {', '.join(sorted(MODEL_ROUTING_MODES))}")
        if aux_review not in AUX_REVIEW_MODES:
            raise ValueError(f"aux_review must be one of: {', '.join(sorted(AUX_REVIEW_MODES))}")

        task = self._task_for_request(request, task_id)
        report = {
            "id": uuid4().hex[:12],
            "created_at": utc_now(),
            "request": request,
            "task_id": task["id"],
            "status": "running",
            "max_steps": max_steps,
            "steps": [],
            "final_response": None,
            "model_routing": {
                "mode": model_routing,
                "primary_model": resolve_role_model(provider_name, model, aux_model, "primary"),
                "auxiliary_model": resolve_role_model(provider_name, model, aux_model, "auxiliary"),
                "aux_review": aux_review,
            },
            "diagnostic": None,
            "state": {"enabled": False, "candidate_count": 0, "written_count": 0, "records": []},
            "totals": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "materialized_files": [],
        }

        for index in range(1, max_steps + 1):
            emit_agent_progress(
                progress_callback,
                {
                    "event": "step_start",
                    "step": index,
                    "max_steps": max_steps,
                    "message": "building minimal context and selecting model",
                },
            )
            step = self._run_step(
                request,
                task["id"],
                index=index,
                max_steps=max_steps,
                prior_steps=report["steps"],
                provider_name=provider_name,
                budget=budget,
                profile=profile,
                model=model,
                aux_model=aux_model,
                model_routing=model_routing,
                aux_review=aux_review,
                base_url=base_url,
                allow_over_budget=allow_over_budget,
                expect_json=expect_json,
                progress_callback=progress_callback,
            )
            report["steps"].append(step)
            emit_agent_progress(
                progress_callback,
                {
                    "event": "step_end",
                    "step": index,
                    "max_steps": max_steps,
                    "status": step.get("status"),
                    "action": (step.get("action") or {}).get("action"),
                    "model_role": step.get("model_role"),
                    "model": step.get("model"),
                    "tokens": step.get("tokens", {}).get("total_tokens", 0),
                },
            )
            add_tokens(report["totals"], step.get("tokens", {}))
            add_tokens(report["totals"], step.get("aux_review", {}).get("tokens", {}))
            if step.get("final_response") is not None:
                report["final_response"] = step["final_response"]
            if step.get("materialized_files"):
                report["materialized_files"].extend(step["materialized_files"])

            if not step["continue"]:
                report["status"] = step["status"]
                report["diagnostic"] = step.get("diagnostic")
                self.tasks.step(task["id"], step["stop_reason"], kind="agent_stop")
                break
        else:
            report["status"] = "stopped"
            self.tasks.step(task["id"], f"Agent loop stopped after {max_steps} step(s).", kind="agent_stop")

        if remember:
            report["state"] = write_agent_run_memory(self.workspace, self.tasks, report)
        report["completed_at"] = utc_now()
        self.workspace.agent_runs_dir.mkdir(parents=True, exist_ok=True)
        Workspace.write_json(self.workspace.agent_runs_dir / f"{report['id']}.json", compact_saved_agent_report(report))
        return report

    def _task_for_request(self, request: str, task_id: str | None) -> dict[str, Any]:
        if task_id:
            task = self.tasks.get(task_id)
            if task.get("status") == "completed":
                raise ValueError(f"Task is completed and cannot receive agent loop steps: {task_id}")
            return task
        return self.tasks.start(request, goal=request)

    def _run_step(
        self,
        request: str,
        task_id: str,
        *,
        index: int,
        max_steps: int,
        prior_steps: list[dict[str, Any]],
        provider_name: str,
        budget: int | None,
        profile: str,
        model: str | None,
        aux_model: str | None,
        model_routing: str,
        aux_review: str,
        base_url: str | None,
        allow_over_budget: bool,
        expect_json: bool,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        plan, effective_budget, budget_block = prepare_agent_step_plan(
            self.workspace,
            request=request,
            task_id=task_id,
            budget=budget,
            profile=profile,
            index=index,
            max_steps=max_steps,
            allow_over_budget=allow_over_budget,
            progress_callback=progress_callback,
        )
        if budget_block is not None:
            return budget_block

        emit_agent_progress(
            progress_callback,
            {
                "event": "context_ready",
                "step": index,
                "max_steps": max_steps,
                "estimated_used": plan["budget"]["estimated_used"],
                "budget_total": plan["budget"]["total"],
                "memory_count": len(plan.get("selection", {}).get("memory", [])),
                "skills": [
                    {"id": item.get("id", ""), "level": item.get("level", "")}
                    for item in plan.get("selection", {}).get("skills", [])[:5]
                ],
            },
        )

        selected_role, routing_reason = select_model_role(
            model_routing=model_routing,
            plan=plan,
            step_index=index,
            prior_steps=prior_steps,
            profile=profile,
        )
        selected_model = resolve_role_model(provider_name, model, aux_model, selected_role)
        emit_agent_progress(
            progress_callback,
            {
                "event": "provider_start",
                "step": index,
                "max_steps": max_steps,
                "model_role": selected_role,
                "model": selected_model,
                "routing_reason": routing_reason,
            },
        )
        review = run_auxiliary_review(
            self.workspace,
            request=request,
            provider_name=provider_name,
            plan=plan,
            selected_role=selected_role,
            selected_model=selected_model,
            aux_model=aux_model,
            base_url=base_url,
            budget=effective_budget,
            profile=profile,
            task_id=task_id,
            allow_over_budget=allow_over_budget,
            aux_review=aux_review,
            routing_reason=routing_reason,
        )

        trace, provider_failure = run_provider_agent_step(
            self.workspace,
            self.tasks,
            request=request,
            task_id=task_id,
            index=index,
            max_steps=max_steps,
            provider_name=provider_name,
            budget=effective_budget,
            profile=profile,
            model=selected_model,
            base_url=base_url,
            allow_over_budget=allow_over_budget,
            expect_json=expect_json,
            plan=plan,
            selected_role=selected_role,
            routing_reason=routing_reason,
            aux_review=review,
        )
        if provider_failure is not None:
            return provider_failure
        attach_trace_outputs(self.tasks, task_id, trace)
        tokens = response_token_counts(trace)

        verifier_ok = bool(trace.get("verifier", {}).get("ok"))
        action, contract_recovered, action_failure = parse_agent_step_action(
            self.tasks,
            task_id=task_id,
            index=index,
            trace=trace,
            expect_json=expect_json,
            verifier_ok=verifier_ok,
            prior_steps=prior_steps,
            plan=plan,
            model_role=selected_role,
            model=selected_model,
            routing_reason=routing_reason,
            aux_review=review,
            tokens=tokens,
        )
        if action_failure is not None:
            return action_failure

        if action["action"] == "respond":
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
            materialized = materialize_code_response_if_needed(self.tools, request, response_text)
            if materialized:
                for trace_result in materialized["traces"]:
                    self.tasks.attach(task_id, "tool", trace_result["id"])
                self.tasks.step(
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
            self.tasks.step(
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
        tool_result = execute_agent_action(self.tools, action)
        self.tasks.attach(task_id, "tool", tool_result["id"])
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
        self.tasks.step(
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
        recovery_tools = auto_recovery_tools(self.tools, request, action, tool_result) if index < max_steps else []
        if recovery_tools:
            for recovery in recovery_tools:
                self.tasks.attach(task_id, "tool", recovery["id"])
            recovery_summary = "; ".join(
                f"{item['tool']}:{summarize_tool_result(item)}"
                for item in recovery_tools
            )
            self.tasks.step(
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


def response_token_counts(trace: dict[str, Any]) -> dict[str, int]:
    response = trace.get("response", {})
    return {
        "input_tokens": int(response.get("input_tokens", 0) or 0),
        "output_tokens": int(response.get("output_tokens", 0) or 0),
        "total_tokens": int(response.get("total_tokens", 0) or 0),
    }


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
        emit_agent_progress(
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


def run_provider_agent_step(
    workspace: Workspace,
    tasks: TaskStore,
    *,
    request: str,
    task_id: str,
    index: int,
    max_steps: int,
    provider_name: str,
    budget: int | None,
    profile: str,
    model: str,
    base_url: str | None,
    allow_over_budget: bool,
    expect_json: bool,
    plan: dict[str, Any],
    selected_role: str,
    routing_reason: str,
    aux_review: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        trace = AgentRunner(workspace).run(
            request,
            provider_name=provider_name,
            budget=budget,
            profile=profile,
            model=model,
            base_url=base_url,
            allow_over_budget=allow_over_budget,
            expect_json=True,
            remember=False,
            task_id=task_id,
            resume=True,
            packet_overrides=build_agent_packet(
                request,
                index,
                max_steps,
                expect_json=expect_json,
                model_role=selected_role,
                routing_reason=routing_reason,
            ),
        )
        return trace, None
    except Exception as exc:
        diagnostic = diagnose_agent_exception(exc)
        tasks.step(
            task_id,
            f"Agent step {index} failed before action execution: {diagnostic['message']}",
            kind="agent_step",
        )
        return {}, agent_step_result(
            index=index,
            status="failed",
            can_continue=False,
            plan=plan,
            stop_reason=f"Agent loop stopped: {diagnostic['category']}.",
            reason=str(exc),
            diagnostic=diagnostic,
            trace_id=None,
            model_role=selected_role,
            model=model,
            routing_reason=routing_reason,
            aux_review=aux_review,
            tool_trace_id=None,
            action=None,
            tokens={},
            verifier_ok=False,
        )


def parse_agent_step_action(
    tasks: TaskStore,
    *,
    task_id: str,
    index: int,
    trace: dict[str, Any],
    expect_json: bool,
    verifier_ok: bool,
    prior_steps: list[dict[str, Any]],
    plan: dict[str, Any],
    model_role: str,
    model: str,
    routing_reason: str,
    aux_review: dict[str, Any],
    tokens: dict[str, int],
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    contract_recovered = False
    try:
        action = parse_agent_action(trace["response"]["text"], expect_json=expect_json)
    except (ValueError, KeyError, TypeError) as exc:
        stop_reason = "provider returned an invalid action payload"
        if not verifier_ok:
            stop_reason = "provider response failed JSON action verification"
        tasks.step(
            task_id,
            f"Agent step {index} needs review: invalid action payload ({compact(str(exc), limit=240)}).",
            kind="agent_step",
            refs={"run_traces": [trace["id"]]},
        )
        return {}, False, agent_step_result(
            index=index,
            status="needs_review",
            can_continue=False,
            plan=plan,
            stop_reason=f"Agent loop stopped: {stop_reason}.",
            reason=str(exc),
            diagnostic={
                "category": "provider_response",
                "message": compact(str(exc), limit=240),
                "suggestion": "Retry with the same task, or use a stricter/stronger model if the provider keeps returning malformed agent actions.",
            },
            trace_id=trace["id"],
            model_role=model_role,
            model=model,
            routing_reason=routing_reason,
            aux_review=aux_review,
            tool_trace_id=None,
            action=None,
            tokens=tokens,
            verifier_ok=verifier_ok,
            contract_recovered=False,
        )
    if not verifier_ok:
        contract_recovered = True
        tasks.step(
            task_id,
            f"Agent step {index} recovered a valid action from non-strict provider JSON.",
            kind="agent_step",
            refs={"run_traces": [trace["id"]]},
        )

    if repeated_agent_action(report_steps=prior_steps, action=action):
        tasks.step(
            task_id,
            f"Agent step {index} stopped: repeated action detected for {action['action']}.",
            kind="agent_step",
            refs={"run_traces": [trace["id"]]},
        )
        return action, contract_recovered, agent_step_result(
            index=index,
            status="needs_review",
            can_continue=False,
            plan=plan,
            stop_reason="Agent loop stopped: repeated identical actions would likely cause a loop.",
            reason="repeated identical actions detected",
            diagnostic={
                "category": "loop_guard",
                "message": "The provider returned the same action repeatedly.",
                "suggestion": "Inspect the saved agent run and linked traces, then retry with more specific instructions, a fresh task, or a larger step budget.",
            },
            trace_id=trace["id"],
            model_role=model_role,
            model=model,
            routing_reason=routing_reason,
            aux_review=aux_review,
            tool_trace_id=None,
            action=summarize_action(action),
            tokens=tokens,
            verifier_ok=verifier_ok,
            contract_recovered=contract_recovered,
        )
    return action, contract_recovered, None


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


def build_agent_packet(
    request: str,
    step_index: int,
    max_steps: int,
    *,
    expect_json: bool = False,
    model_role: str = "primary",
    routing_reason: str = "",
) -> dict[str, Any]:
    respond_schema: dict[str, Any] = {
        "action": "respond",
        "message": "string",
        "reason": "string optional",
    }
    if expect_json:
        respond_schema["message"] = "string containing compact JSON text"
    rules = [
        "Return only valid JSON with no surrounding commentary.",
        "Choose exactly one action.",
        "Use at most one tool action in a step.",
        "Use respond when enough information is already available.",
        "For requests to write, create, generate, implement, or modify code/scripts/apps/files, do not answer with a code block only; choose create_file, write_file, patch_file, append_file, or batch_patch so the code is saved in the workspace.",
        "Prefer create_file for new files because it refuses to overwrite existing files. Use write_file only when the request clearly allows replacing the whole file.",
        "Use file_info or list_dir before writing if you are unsure whether a path already exists.",
        "If the user asks for code but does not provide a filename, infer a safe filename under generated/ and use create_file.",
        "Respect policy-gated tools; do not ask for destructive operations.",
        "Before choosing run_command, check runtime.command_policy.allowed_roots and only use an allowed command root.",
        "When the user asks to run tests, verify, build, lint, or install and runtime.project.commands contains the matching command, use that exact project command.",
        "If the user's requested command root is outside runtime.command_policy.allowed_roots, respond with the restriction instead of retrying the blocked command.",
        "Prefer reusing task brief summaries instead of repeating a completed tool action.",
        "If a patch or verification step fails, check any recovery read summaries before deciding the next action.",
        "When the user describes a block between markers, use patch_file with start_anchor and end_anchor instead of rewriting the whole file.",
        "When the user asks for multiple file edits, prefer one batch_patch action with an edits array.",
        "Use mcp_call only for enabled MCP servers and discovered tools listed in runtime.mcp.servers; never invent server or tool names.",
    ]
    if is_patch_verify_request(request):
        rules.append("When the request asks for a patch and a verification command, patch first, then run the command, then respond.")
    if is_write_verify_request(request):
        rules.append("When the request asks for a file write and a verification command, write the file first, then run the command, then respond.")
    return {
        "agent": {
            "mode": "tool_planning_v8",
            "step_index": step_index,
            "max_steps": max_steps,
            "model_role": model_role,
            "routing_reason": routing_reason,
            "available_tools": [
                {
                    "name": "respond",
                    "description": "Return the final user-facing response and stop the loop.",
                    "schema": respond_schema,
                },
                {
                    "name": "list_dir",
                    "description": "List one workspace directory before choosing files to read or edit.",
                    "schema": {
                        "action": "list_dir",
                        "path": "relative directory path, default .",
                        "limit": "optional integer, <= 200",
                        "reason": "string optional",
                    },
                },
                {
                    "name": "file_info",
                    "description": "Check whether one workspace path exists and whether it is a file or directory.",
                    "schema": {
                        "action": "file_info",
                        "path": "relative file or directory path",
                        "reason": "string optional",
                    },
                },
                {
                    "name": "read_file",
                    "description": "Read one workspace file through policy checks.",
                    "schema": {
                        "action": "read_file",
                        "path": "relative file path",
                        "max_chars": f"optional integer, <= {MAX_CAPTURE_CHARS}",
                        "reason": "string optional",
                    },
                },
                {
                    "name": "create_file",
                    "description": "Create one new workspace file. Fails safely if the file already exists.",
                    "schema": {
                        "action": "create_file",
                        "path": "relative file path",
                        "text": "complete file contents",
                        "reason": "string optional",
                    },
                },
                {
                    "name": "write_file",
                    "description": "Create or overwrite one workspace file through policy checks. Prefer create_file for new files and patch_file for edits.",
                    "schema": {
                        "action": "write_file",
                        "path": "relative file path",
                        "text": "complete file contents",
                        "reason": "string optional",
                    },
                },
                {
                    "name": "append_file",
                    "description": "Append text to the end of one workspace file. Can create the file unless create=false.",
                    "schema": {
                        "action": "append_file",
                        "path": "relative file path",
                        "text": "text to append",
                        "create": "optional boolean, default true",
                        "reason": "string optional",
                    },
                },
                {
                    "name": "patch_file",
                    "description": "Apply a structured text replacement to a workspace file.",
                    "schema": {
                        "action": "patch_file",
                        "path": "relative file path",
                        "new": "replacement text",
                        "old": "exact old text for text replacement mode",
                        "replace_all": "optional boolean; use true to replace every match of old",
                        "occurrence": "optional integer >= 1; replace only the nth match of old",
                        "start_anchor": "optional exact start marker for anchor block mode",
                        "end_anchor": "optional exact end marker for anchor block mode",
                        "include_anchors": "optional boolean; replace anchors together with the block body",
                        "reason": "string optional",
                    },
                },
                {
                    "name": "batch_patch",
                    "description": "Apply multiple structured patches as one batch tool step.",
                    "schema": {
                        "action": "batch_patch",
                        "edits": [
                            {
                                "path": "relative file path",
                                "new": "replacement text",
                                "old": "exact old text for text replacement mode",
                                "replace_all": "optional boolean",
                                "occurrence": "optional integer >= 1",
                                "start_anchor": "optional exact start marker for anchor block mode",
                                "end_anchor": "optional exact end marker for anchor block mode",
                                "include_anchors": "optional boolean",
                            }
                        ],
                        "reason": "string optional",
                    },
                },
                {
                    "name": "run_command",
                    "description": "Run one safe non-interactive command through policy checks.",
                    "schema": {
                        "action": "run_command",
                        "command": "command string",
                        "timeout_seconds": "optional integer between 1 and 300",
                        "reason": "string optional",
                    },
                },
                {
                    "name": "mcp_call",
                    "description": "Call one discovered MCP tool through the configured stdio MCP bridge.",
                    "schema": {
                        "action": "mcp_call",
                        "server": "MCP server name from runtime.mcp.servers",
                        "tool": "tool name from that server's discovered tools",
                        "arguments": "JSON object passed to the MCP tool",
                        "timeout_seconds": "optional integer between 1 and 60",
                        "reason": "string optional",
                    },
                },
            ],
            "response_contract": {
                "type": "json_object",
                "rules": rules,
            },
        }
    }


def attach_trace_outputs(tasks: TaskStore, task_id: str, trace: dict[str, Any]) -> None:
    tasks.attach(task_id, "run", trace["id"])
    for record in trace.get("state", {}).get("records", []):
        tasks.attach(task_id, "memory", record["id"])


def auto_recovery_tools(
    executor: ToolExecutor,
    request: str,
    action: dict[str, Any],
    tool_result: dict[str, Any],
) -> list[dict[str, Any]]:
    if tool_result["blocked"] or tool_result["ok"]:
        return []
    if action["action"] == "patch_file":
        return [executor.read_file(action["path"], max_chars=min(4000, MAX_CAPTURE_CHARS))]
    if action["action"] == "run_command":
        explicit_target = recovery_target_path(request)
        targets = [explicit_target] if explicit_target else command_failure_target_paths(executor.workspace, tool_result, limit=3)
        return [
            executor.read_file(target, max_chars=min(4000, MAX_CAPTURE_CHARS))
            for target in targets
            if target
        ]
    return []


def recovery_target_path(request: str) -> str | None:
    anchor_patch = extract_anchor_patch_instruction(request)
    if anchor_patch:
        return anchor_patch[0]
    patch = extract_patch_instruction(request)
    if patch:
        return patch[0]
    write = extract_write_instruction(request)
    if write:
        return write[0]
    return None


def diagnose_agent_exception(exc: Exception) -> dict[str, str]:
    message = compact(str(exc), limit=500) or exc.__class__.__name__
    lower = message.casefold()
    if (
        "missing akernel_openai_base_url" in lower
        or "missing akernel_openai_api_key" in lower
        or "missing context_kernel_openai_base_url" in lower
        or "missing context_kernel_openai_api_key" in lower
    ):
        return {
            "category": "provider_configuration",
            "message": message,
            "suggestion": "Run `akernel setup` in the project, then set API key, base URL, primary model, and auxiliary model.",
        }
    if "provider http 401" in lower or "provider http 403" in lower:
        return {
            "category": "provider_auth",
            "message": message,
            "suggestion": "Check `AKERNEL_OPENAI_API_KEY` in the project `.env` and confirm the endpoint accepts it.",
        }
    if "provider http 404" in lower:
        return {
            "category": "provider_endpoint",
            "message": message,
            "suggestion": "Check that the base URL includes `/v1` and that the selected model exists on this endpoint.",
        }
    if "provider http 429" in lower:
        return {
            "category": "provider_rate_limit",
            "message": message,
            "suggestion": "Wait and retry, or switch to a lower-cost auxiliary model for planning steps.",
        }
    if "provider http 5" in lower:
        return {
            "category": "provider_server",
            "message": message,
            "suggestion": "The provider returned a server error. Retry later or verify the endpoint health with `akernel models --provider openai`.",
        }
    if "provider network" in lower or "timed out" in lower or "connection" in lower:
        return {
            "category": "provider_network",
            "message": message,
            "suggestion": (
                "Retry the task; if the endpoint is slow, increase `AKERNEL_OPENAI_TIMEOUT_SECONDS` "
                "or `AKERNEL_OPENAI_MAX_RETRIES`, then verify with `akernel models --provider openai`."
            ),
        }
    if "provider returned invalid json" in lower:
        return {
            "category": "provider_protocol",
            "message": message,
            "suggestion": "The endpoint did not return OpenAI-compatible JSON. Check the base URL and provider compatibility.",
        }
    return {
        "category": "runtime_error",
        "message": message,
        "suggestion": "Inspect the saved run, then retry with `--provider mock` to separate runtime issues from provider issues.",
    }


def diagnose_tool_result(tool_result: dict[str, Any]) -> dict[str, str]:
    tool = str(tool_result.get("tool") or "tool")
    summary = summarize_tool_result(tool_result)
    if tool_result.get("blocked"):
        return {
            "category": "policy_block",
            "message": summary,
            "suggestion": "Use an allowed workspace path or command root, or update `.akernel/config.json` if the command is intentionally safe.",
        }
    if tool == "run_command":
        return {
            "category": "command_failed",
            "message": summary,
            "suggestion": "Inspect stdout/stderr in the linked tool trace, fix the underlying issue, then rerun the task.",
        }
    if tool == "mcp_call":
        return {
            "category": "mcp_call_failed",
            "message": summary,
            "suggestion": "Run `akernel mcp list` and `akernel mcp refresh <name>`, then retry with an enabled discovered tool.",
        }
    return {
        "category": "tool_failed",
        "message": summary,
        "suggestion": "Inspect the linked tool trace and retry with a narrower file path or patch instruction.",
    }


def command_failure_target_path(workspace: Workspace, tool_result: dict[str, Any]) -> str | None:
    targets = command_failure_target_paths(workspace, tool_result, limit=1)
    return targets[0] if targets else None


def command_failure_target_paths(workspace: Workspace, tool_result: dict[str, Any], *, limit: int = 3) -> list[str]:
    output = tool_result.get("output", {})
    text = "\n".join(
        str(part)
        for part in [output.get("stderr", ""), output.get("stdout", ""), tool_result.get("error", "")]
        if part
    )
    if not text:
        return []
    candidates = python_failure_path_candidates(text)
    targets: list[str] = []
    seen: set[str] = set()
    for candidate in reversed(candidates):
        normalized = candidate.strip().strip('"').strip("'").replace("\\", "/")
        if "=" in normalized:
            normalized = normalized.rsplit("=", 1)[-1]
        if any(part in normalized for part in ["/.venv/", "/site-packages/", "/.akernel/"]):
            continue
        path = Path(normalized)
        if path.is_absolute():
            try:
                normalized = path.resolve().relative_to(workspace.root).as_posix()
            except ValueError:
                continue
        else:
            normalized = normalized.lstrip("./")
        if normalized in seen:
            continue
        seen.add(normalized)
        targets.append(normalized)
        if len(targets) >= limit:
            break
    return targets


def python_failure_path_candidates(text: str) -> list[str]:
    candidates = re.findall(r"File\s+\"([^\"]+\.py)\",\s+line\s+\d+", text)
    candidates.extend(re.findall(r"((?:[A-Za-z]:)?[^\s:]+\.py):\d+", text))
    return candidates


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


def adaptive_context_budget(plan: dict[str, Any]) -> int:
    used = int(plan.get("budget", {}).get("estimated_used", 0) or 0)
    current = int(plan.get("budget", {}).get("total", 0) or 0)
    cushion = max(800, int(used * 0.25))
    return min(32000, max(current * 2, used + cushion, 6000))


def select_model_role(
    *,
    model_routing: str,
    plan: dict[str, Any],
    step_index: int,
    prior_steps: list[dict[str, Any]],
    profile: str,
) -> tuple[str, str]:
    if model_routing in {"primary", "auxiliary"}:
        return model_routing, f"forced by --model-routing {model_routing}"

    route = plan.get("route", {})
    complexity = route.get("complexity", "low")
    warnings = plan.get("warnings", [])
    serious_warnings = [warning for warning in warnings if is_primary_required_warning(str(warning))]
    if profile == "deep":
        return "primary", "deep profile keeps reasoning on the primary model"
    if complexity == "high":
        return "primary", "high-complexity route requires the primary model"
    if serious_warnings:
        return "primary", "policy or budget warnings require the primary model"
    if prior_steps:
        return "primary", "synthesis after tool/context steps stays on the primary model"
    if step_index == 1 and complexity in {"low", "medium"}:
        return "auxiliary", f"{complexity}-complexity first-step planning is delegated to the auxiliary model"
    return "primary", "default fallback to the primary model"


def is_primary_required_warning(warning: str) -> bool:
    text = warning.casefold()
    return any(term in text for term in ["over budget", "policy", "blocked", "destructive", "unsafe", "do not execute"])


def resolve_role_model(provider_name: str, model: str | None, aux_model: str | None, role: str) -> str | None:
    if provider_name != "openai":
        return aux_model if role == "auxiliary" else model
    if role == "auxiliary":
        return aux_model or env_value("AKERNEL_OPENAI_AUX_MODEL") or DEFAULT_AUXILIARY_MODEL
    return model or env_value("AKERNEL_OPENAI_MODEL") or DEFAULT_PRIMARY_MODEL


def run_auxiliary_review(
    workspace: Workspace,
    *,
    request: str,
    provider_name: str,
    plan: dict[str, Any],
    selected_role: str,
    selected_model: str | None,
    aux_model: str | None,
    base_url: str | None,
    budget: int | None,
    profile: str,
    task_id: str,
    allow_over_budget: bool,
    aux_review: str,
    routing_reason: str,
) -> dict[str, Any]:
    resolved_aux = resolve_role_model(provider_name, selected_model, aux_model, "auxiliary")
    if aux_review == "off":
        return {"enabled": False, "reason": "disabled by --aux-review off"}
    if aux_review == "auto" and selected_role != "primary":
        return {"enabled": False, "reason": "auto review only runs before primary-model steps"}
    if not resolved_aux:
        return {"enabled": False, "reason": "no auxiliary model configured for this provider"}

    try:
        trace = AgentRunner(workspace).run(
            request,
            provider_name=provider_name,
            budget=budget,
            profile=profile,
            model=resolved_aux,
            base_url=base_url,
            allow_over_budget=allow_over_budget,
            expect_json=True,
            remember=False,
            task_id=task_id,
            resume=True,
            packet_overrides=build_aux_review_packet(
                plan,
                selected_role=selected_role,
                selected_model=selected_model,
                aux_model=resolved_aux,
                routing_reason=routing_reason,
            ),
        )
    except Exception as exc:
        diagnostic = diagnose_agent_exception(exc)
        return {
            "enabled": True,
            "trace_id": None,
            "model": resolved_aux,
            "ok": False,
            "risk": "medium",
            "recommendation": "use_primary",
            "notes": [diagnostic["message"]],
            "tokens": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "verifier_ok": False,
            "diagnostic": diagnostic,
        }
    parsed = parse_aux_review(trace.get("response", {}).get("text", ""))
    tokens = trace.get("response", {})
    return {
        "enabled": True,
        "trace_id": trace["id"],
        "model": resolved_aux,
        "ok": parsed["ok"],
        "risk": parsed["risk"],
        "recommendation": parsed["recommendation"],
        "notes": parsed["notes"],
        "tokens": {
            "input_tokens": tokens.get("input_tokens", 0),
            "output_tokens": tokens.get("output_tokens", 0),
            "total_tokens": tokens.get("total_tokens", 0),
        },
        "verifier_ok": bool(trace.get("verifier", {}).get("ok")),
    }


def build_aux_review_packet(
    plan: dict[str, Any],
    *,
    selected_role: str,
    selected_model: str | None,
    aux_model: str | None,
    routing_reason: str,
) -> dict[str, Any]:
    route = plan.get("route", {})
    return {
        "agent": {
            "mode": "aux_review_v1",
            "review": {
                "selected_role": selected_role,
                "selected_model": selected_model,
                "aux_model": aux_model,
                "routing_reason": routing_reason,
                "route_mode": route.get("mode"),
                "complexity": route.get("complexity"),
                "warnings": plan.get("warnings", []),
            },
            "response_contract": {
                "type": "json_object",
                "rules": [
                    "Return only valid JSON.",
                    "Schema: {\"ok\": boolean, \"risk\": \"low|medium|high\", \"recommendation\": \"continue|use_primary|reduce_context|stop\", \"notes\": [\"short note\"]}.",
                    "Be conservative about policy or budget risk, but do not invent missing facts.",
                ],
            },
        }
    }


def parse_aux_review(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    risk = str(data.get("risk", "medium")).strip().lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    recommendation = str(data.get("recommendation", "continue")).strip().lower()
    if recommendation not in {"continue", "use_primary", "reduce_context", "stop"}:
        recommendation = "continue"
    notes = data.get("notes", [])
    if not isinstance(notes, list):
        notes = [str(notes)]
    return {
        "ok": bool(data.get("ok", True)),
        "risk": risk,
        "recommendation": recommendation,
        "notes": [compact(str(item), limit=160) for item in notes[:5]],
    }


def final_tool_stop_reason(result: dict[str, Any], *, recovery_tools: list[dict[str, Any]], max_steps: int) -> str:
    if result["blocked"]:
        return "Agent loop stopped: the final tool action was blocked by policy."
    if not result["ok"]:
        if recovery_tools:
            return "Agent loop stopped: recovery context was prepared, but no loop step remained to use it."
        return "Agent loop stopped: the final tool action failed and needs review."
    return f"Agent loop stopped after {max_steps} step(s)."


def is_patch_verify_request(request: str) -> bool:
    lower = request.casefold()
    has_patch = "patch " in lower
    has_command = "run command " in lower or " run `" in lower or "verify with command " in lower
    return has_patch and has_command


def is_write_verify_request(request: str) -> bool:
    lower = request.casefold()
    has_write = "write " in lower or "create " in lower
    has_command = "run command " in lower or " run `" in lower or "verify with command " in lower
    return has_write and has_command
