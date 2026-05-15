from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

from .memory import MemoryStore
from .mcp import call_mcp_tool
from .models import utc_now
from .planner import ExecutionPlanner
from .providers import env_value, extract_anchor_patch_instruction, extract_patch_instruction, extract_write_instruction
from .runner import AgentRunner
from .skills import extract_json_object
from .storage import Workspace
from .tasks import TaskStore
from .tools import MAX_CAPTURE_CHARS, ToolExecutor


TOOL_ACTIONS = {"read_file", "write_file", "patch_file", "batch_patch", "run_command", "mcp_call"}
ALLOWED_ACTIONS = TOOL_ACTIONS | {"respond"}
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
        effective_budget = budget
        plan = ExecutionPlanner(self.workspace).plan(
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
            plan = ExecutionPlanner(self.workspace).plan(
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
            return {
                "index": index,
                "status": "blocked",
                "continue": False,
                "stop_reason": "Agent loop stopped: context packet is over budget.",
                "reason": "context packet is over budget",
                "diagnostic": diagnostic,
                "plan": summarize_plan(plan),
                "trace_id": None,
                "tool_trace_id": None,
                "action": None,
                "tokens": {},
                "verifier_ok": False,
            }

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

        try:
            trace = AgentRunner(self.workspace).run(
                request,
                provider_name=provider_name,
                budget=effective_budget,
                profile=profile,
                model=selected_model,
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
        except Exception as exc:
            diagnostic = diagnose_agent_exception(exc)
            self.tasks.step(
                task_id,
                f"Agent step {index} failed before action execution: {diagnostic['message']}",
                kind="agent_step",
            )
            return {
                "index": index,
                "status": "failed",
                "continue": False,
                "stop_reason": f"Agent loop stopped: {diagnostic['category']}.",
                "reason": str(exc),
                "diagnostic": diagnostic,
                "plan": summarize_plan(plan),
                "trace_id": None,
                "model_role": selected_role,
                "model": selected_model,
                "routing_reason": routing_reason,
                "aux_review": review,
                "tool_trace_id": None,
                "action": None,
                "tokens": {},
                "verifier_ok": False,
            }
        attach_trace_outputs(self.tasks, task_id, trace)
        tokens = trace.get("response", {})

        verifier_ok = bool(trace.get("verifier", {}).get("ok"))
        contract_recovered = False
        try:
            action = parse_agent_action(trace["response"]["text"], expect_json=expect_json)
        except (ValueError, KeyError, TypeError) as exc:
            stop_reason = "provider returned an invalid action payload"
            if not verifier_ok:
                stop_reason = "provider response failed JSON action verification"
            self.tasks.step(
                task_id,
                f"Agent step {index} needs review: invalid action payload ({compact(str(exc), limit=240)}).",
                kind="agent_step",
                refs={"run_traces": [trace["id"]]},
            )
            return {
                "index": index,
                "status": "needs_review",
                "continue": False,
                "stop_reason": f"Agent loop stopped: {stop_reason}.",
                "reason": str(exc),
                "diagnostic": {
                    "category": "provider_response",
                    "message": compact(str(exc), limit=240),
                    "suggestion": "Retry with the same task, or use a stricter/stronger model if the provider keeps returning malformed agent actions.",
                },
                "plan": summarize_plan(plan),
                "trace_id": trace["id"],
                "model_role": selected_role,
                "model": selected_model,
                "routing_reason": routing_reason,
                "aux_review": review,
                "tool_trace_id": None,
                "action": None,
                "tokens": {
                    "input_tokens": tokens.get("input_tokens", 0),
                    "output_tokens": tokens.get("output_tokens", 0),
                    "total_tokens": tokens.get("total_tokens", 0),
                },
                "verifier_ok": verifier_ok,
                "contract_recovered": False,
            }
        if not verifier_ok:
            contract_recovered = True
            self.tasks.step(
                task_id,
                f"Agent step {index} recovered a valid action from non-strict provider JSON.",
                kind="agent_step",
                refs={"run_traces": [trace["id"]]},
            )

        repeated_action = repeated_agent_action(report_steps=prior_steps, action=action)
        if repeated_action:
            self.tasks.step(
                task_id,
                f"Agent step {index} stopped: repeated action detected for {action['action']}.",
                kind="agent_step",
                refs={"run_traces": [trace["id"]]},
            )
            return {
                "index": index,
                "status": "needs_review",
                "continue": False,
                "stop_reason": "Agent loop stopped: repeated identical actions would likely cause a loop.",
                "reason": "repeated identical actions detected",
                "diagnostic": {
                    "category": "loop_guard",
                    "message": "The provider returned the same action repeatedly.",
                    "suggestion": "Inspect the saved agent run and linked traces, then retry with more specific instructions, a fresh task, or a larger step budget.",
                },
                "plan": summarize_plan(plan),
                "trace_id": trace["id"],
                "model_role": selected_role,
                "model": selected_model,
                "routing_reason": routing_reason,
                "aux_review": review,
                "tool_trace_id": None,
                "action": summarize_action(action),
                "tokens": {
                    "input_tokens": tokens.get("input_tokens", 0),
                    "output_tokens": tokens.get("output_tokens", 0),
                    "total_tokens": tokens.get("total_tokens", 0),
                },
                "verifier_ok": verifier_ok,
                "contract_recovered": contract_recovered,
            }

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
            return {
                "index": index,
                "status": "responded",
                "continue": False,
                "stop_reason": "Agent loop stopped: a final response was produced.",
                "plan": summarize_plan(plan),
                "trace_id": trace["id"],
                "model_role": selected_role,
                "model": selected_model,
                "routing_reason": routing_reason,
                "aux_review": review,
                "tool_trace_id": None,
                "action": summarize_action(action),
                "tokens": {
                    "input_tokens": tokens.get("input_tokens", 0),
                    "output_tokens": tokens.get("output_tokens", 0),
                    "total_tokens": tokens.get("total_tokens", 0),
                },
                "verifier_ok": verifier_ok,
                "contract_recovered": contract_recovered,
                "final_response": response_text,
                "materialized_files": materialized["paths"] if materialized else [],
            }

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
        return {
            "index": index,
            "status": status,
            "continue": can_continue,
            "stop_reason": final_tool_stop_reason(tool_result, recovery_tools=recovery_tools, max_steps=max_steps) if not can_continue else "",
            "diagnostic": diagnostic,
            "plan": summarize_plan(plan),
            "trace_id": trace["id"],
            "model_role": selected_role,
            "model": selected_model,
            "routing_reason": routing_reason,
            "aux_review": review,
            "tool_trace_id": tool_result["id"],
            "action": summarize_action(action),
            "tool": {
                "id": tool_result["id"],
                "name": tool_result["tool"],
                "ok": tool_result["ok"],
                "blocked": tool_result["blocked"],
                "error": tool_result.get("error"),
                "summary": tool_summary,
            },
            "recovery_tools": [
                {
                    "id": item["id"],
                    "name": item["tool"],
                    "ok": item["ok"],
                    "blocked": item["blocked"],
                    "summary": summarize_tool_result(item),
                }
                for item in recovery_tools
            ],
            "tokens": {
                "input_tokens": tokens.get("input_tokens", 0),
                "output_tokens": tokens.get("output_tokens", 0),
                "total_tokens": tokens.get("total_tokens", 0),
            },
            "verifier_ok": verifier_ok,
            "contract_recovered": contract_recovered,
        }


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
        "For requests to write, create, generate, implement, or modify code/scripts/apps/files, do not answer with a code block only; choose write_file, patch_file, or batch_patch so the code is saved in the workspace.",
        "If the user asks for code but does not provide a filename, infer a safe filename under generated/ and use write_file.",
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
                    "name": "write_file",
                    "description": "Create or overwrite one workspace file through policy checks.",
                    "schema": {
                        "action": "write_file",
                        "path": "relative file path",
                        "text": "complete file contents",
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


def parse_agent_action(text: str, *, expect_json: bool = False) -> dict[str, Any]:
    action = normalize_agent_action_payload(extract_json_object(text))
    action_name = str(action.get("action", "")).strip().lower()
    if action_name not in ALLOWED_ACTIONS:
        raise ValueError(f"Unsupported action: {action_name or '[missing]'}")
    if action_name == "respond":
        message = action.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("respond action requires a non-empty string message")
        if expect_json:
            json.loads(message)
        return {
            "action": "respond",
            "message": message.strip(),
            "reason": compact(str(action.get("reason", "")), limit=240),
        }
    if action_name == "read_file":
        path = require_non_empty_string(action, "path")
        max_chars = clamp_int(action.get("max_chars", MAX_CAPTURE_CHARS), default=MAX_CAPTURE_CHARS, minimum=1, maximum=MAX_CAPTURE_CHARS)
        return {
            "action": "read_file",
            "path": path,
            "max_chars": max_chars,
            "reason": compact(str(action.get("reason", "")), limit=240),
        }
    if action_name == "write_file":
        path = require_non_empty_string(action, "path")
        text = str(action.get("text", ""))
        return {
            "action": "write_file",
            "path": path,
            "text": text,
            "reason": compact(str(action.get("reason", "")), limit=240),
        }
    if action_name == "patch_file":
        path = require_non_empty_string(action, "path")
        new = str(action.get("new", ""))
        start_anchor = optional_non_empty_string(action, "start_anchor")
        end_anchor = optional_non_empty_string(action, "end_anchor")
        include_anchors = bool(action.get("include_anchors", False))
        anchor_mode = start_anchor is not None or end_anchor is not None
        if anchor_mode:
            if not start_anchor or not end_anchor:
                raise ValueError("patch_file anchor mode requires both start_anchor and end_anchor")
            if action.get("old"):
                raise ValueError("patch_file anchor mode cannot combine old with start/end anchors")
            if action.get("replace_all") or action.get("occurrence") not in {None, ""}:
                raise ValueError("patch_file anchor mode cannot combine replace_all or occurrence")
            return {
                "action": "patch_file",
                "path": path,
                "new": new,
                "start_anchor": start_anchor,
                "end_anchor": end_anchor,
                "include_anchors": include_anchors,
                "reason": compact(str(action.get("reason", "")), limit=240),
            }

        old = require_non_empty_string(action, "old")
        replace_all = bool(action.get("replace_all", False))
        occurrence = optional_int(action.get("occurrence"), minimum=1)
        if replace_all and occurrence is not None:
            raise ValueError("patch_file cannot combine replace_all with occurrence")
        return {
            "action": "patch_file",
            "path": path,
            "old": old,
            "new": new,
            "replace_all": replace_all,
            "occurrence": occurrence,
            "include_anchors": False,
            "reason": compact(str(action.get("reason", "")), limit=240),
        }
    if action_name == "batch_patch":
        edits = action.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ValueError("batch_patch requires a non-empty edits array")
        return {
            "action": "batch_patch",
            "edits": [parse_patch_edit(edit) for edit in edits],
            "reason": compact(str(action.get("reason", "")), limit=240),
        }
    if action_name == "mcp_call":
        server = require_non_empty_string(action, "server")
        tool = require_non_empty_string(action, "tool")
        arguments = action.get("arguments", action.get("tool_arguments", {}))
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            parsed_arguments = parse_arguments_object(arguments)
            if not parsed_arguments and arguments not in ("", None):
                raise ValueError("mcp_call arguments must be a JSON object")
            arguments = parsed_arguments
        timeout_seconds = clamp_int(action.get("timeout_seconds", 10), default=10, minimum=1, maximum=60)
        return {
            "action": "mcp_call",
            "server": server,
            "tool": tool,
            "arguments": arguments,
            "timeout_seconds": timeout_seconds,
            "reason": compact(str(action.get("reason", "")), limit=240),
        }
    command = require_non_empty_string(action, "command")
    timeout_seconds = clamp_int(action.get("timeout_seconds", 30), default=30, minimum=1, maximum=300)
    return {
        "action": "run_command",
        "command": command,
        "timeout_seconds": timeout_seconds,
    "reason": compact(str(action.get("reason", "")), limit=240),
    }


def normalize_agent_action_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept common one-tool JSON shapes while preserving the canonical contract."""
    action = unwrap_single_action_payload(payload)
    action = unwrap_tool_call_payload(action)

    if not isinstance(action, dict):
        raise ValueError("Agent action payload must be a JSON object")

    action_name = (
        action.get("action")
        or action.get("tool")
        or action.get("name")
        or action.get("tool_name")
    )
    normalized_action_name = normalize_action_name(str(action_name)) if action_name is not None and not isinstance(action_name, dict) else None
    raw_arguments = parse_arguments_object(action.get("arguments"))
    mcp_arguments_are_action_args = (
        normalized_action_name == "mcp_call"
        and ("server" not in action or "tool" not in action)
        and {"server", "tool"}.issubset(raw_arguments.keys())
    )
    if mcp_arguments_are_action_args:
        nested_args = raw_arguments
    else:
        nested_arg_keys = ("args", "input", "parameters") if normalized_action_name == "mcp_call" else ("arguments", "args", "input", "parameters")
        nested_args = first_dict(action, *nested_arg_keys)
    if isinstance(action_name, dict):
        nested_args = nested_args or first_dict(action_name, "arguments", "args", "input", "parameters")
        action_name = action_name.get("name") or action_name.get("action") or action_name.get("tool")
        normalized_action_name = normalize_action_name(str(action_name)) if action_name is not None else None

    normalized: dict[str, Any] = {}
    if nested_args:
        normalized.update(nested_args)
    omitted = {"arguments", "args", "input", "parameters"} if mcp_arguments_are_action_args else {"args", "input", "parameters"} if normalized_action_name == "mcp_call" else {"arguments", "args", "input", "parameters"}
    normalized.update({key: value for key, value in action.items() if key not in omitted})
    if normalized_action_name is not None:
        normalized["action"] = normalized_action_name
    return normalized


def unwrap_single_action_payload(payload: dict[str, Any]) -> Any:
    for key in ["action", "tool", "tool_call"]:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    for key in ["actions", "steps"]:
        value = payload.get(key)
        if isinstance(value, list) and len(value) == 1:
            return value[0]
    return payload


def unwrap_tool_call_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    tool_calls = payload.get("tool_calls")
    if isinstance(tool_calls, list) and len(tool_calls) == 1:
        return unwrap_tool_call_payload(tool_calls[0])
    function = payload.get("function")
    if isinstance(function, dict):
        arguments = parse_arguments_object(function.get("arguments"))
        result = dict(arguments)
        result["action"] = function.get("name")
        return result
    return payload


def first_dict(data: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            return value
        parsed = parse_arguments_object(value)
        if parsed:
            return parsed
    return None


def parse_arguments_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_action_name(name: str) -> str:
    normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "read": "read_file",
        "file_read": "read_file",
        "write": "write_file",
        "file_write": "write_file",
        "patch": "patch_file",
        "edit_file": "patch_file",
        "batch_edit": "batch_patch",
        "shell": "run_command",
        "exec": "run_command",
        "execute": "run_command",
        "command": "run_command",
        "mcp": "mcp_call",
        "mcp_tool": "mcp_call",
        "tool_call_mcp": "mcp_call",
        "final": "respond",
        "final_answer": "respond",
        "answer": "respond",
    }
    return aliases.get(normalized, normalized)


def parse_patch_edit(edit: Any) -> dict[str, Any]:
    if not isinstance(edit, dict):
        raise ValueError("batch_patch edits must be objects")
    path = require_non_empty_string(edit, "path")
    new = str(edit.get("new", ""))
    start_anchor = optional_non_empty_string(edit, "start_anchor")
    end_anchor = optional_non_empty_string(edit, "end_anchor")
    include_anchors = bool(edit.get("include_anchors", False))
    anchor_mode = start_anchor is not None or end_anchor is not None
    if anchor_mode:
        if not start_anchor or not end_anchor:
            raise ValueError("batch_patch anchor edits require both start_anchor and end_anchor")
        if edit.get("old"):
            raise ValueError("batch_patch anchor edits cannot combine old with start/end anchors")
        if edit.get("replace_all") or edit.get("occurrence") not in {None, ""}:
            raise ValueError("batch_patch anchor edits cannot combine replace_all or occurrence")
        return {
            "path": path,
            "new": new,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "include_anchors": include_anchors,
        }

    old = require_non_empty_string(edit, "old")
    replace_all = bool(edit.get("replace_all", False))
    occurrence = optional_int(edit.get("occurrence"), minimum=1)
    if replace_all and occurrence is not None:
        raise ValueError("batch_patch edits cannot combine replace_all with occurrence")
    return {
        "path": path,
        "old": old,
        "new": new,
        "replace_all": replace_all,
        "occurrence": occurrence,
        "include_anchors": False,
    }


def execute_agent_action(executor: ToolExecutor, action: dict[str, Any]) -> dict[str, Any]:
    if action["action"] == "read_file":
        return executor.read_file(action["path"], max_chars=action["max_chars"])
    if action["action"] == "write_file":
        return executor.write_file(action["path"], action["text"])
    if action["action"] == "patch_file":
        return executor.patch_file(
            action["path"],
            action.get("old", ""),
            action["new"],
            replace_all=bool(action.get("replace_all", False)),
            occurrence=action.get("occurrence"),
            start_anchor=action.get("start_anchor"),
            end_anchor=action.get("end_anchor"),
            include_anchors=bool(action.get("include_anchors", False)),
        )
    if action["action"] == "batch_patch":
        return executor.batch_patch(action["edits"])
    if action["action"] == "run_command":
        return executor.run_command(action["command"], timeout_seconds=action["timeout_seconds"])
    if action["action"] == "mcp_call":
        subject = f"{action['server']}.{action['tool']}"
        try:
            call = call_mcp_tool(
                executor.workspace,
                action["server"],
                action["tool"],
                action.get("arguments", {}),
                timeout_seconds=action.get("timeout_seconds", 10),
            )
        except Exception as exc:
            return executor.record_external_tool(
                "mcp_call",
                subject=subject,
                output={
                    "server": action["server"],
                    "tool": action["tool"],
                    "arguments": action.get("arguments", {}),
                },
                ok=False,
                error=str(exc),
            )
        return executor.record_external_tool("mcp_call", subject=subject, output=call, ok=True)
    raise ValueError(f"Unsupported tool action: {action['action']}")


def action_progress_label(action: dict[str, Any]) -> str:
    action_name = str(action.get("action", ""))
    if action_name == "read_file":
        return "reading file"
    if action_name == "write_file":
        return "creating or updating file"
    if action_name == "patch_file":
        return "applying file patch"
    if action_name == "batch_patch":
        return "applying multi-file patch"
    if action_name == "run_command":
        return "running command"
    if action_name == "mcp_call":
        return "calling MCP tool"
    return action_name or "running action"


def action_progress_target(action: dict[str, Any]) -> str:
    action_name = str(action.get("action", ""))
    if action_name in {"read_file", "write_file", "patch_file"}:
        return compact(str(action.get("path", "")), limit=160)
    if action_name == "batch_patch":
        edits = action.get("edits", [])
        paths = [str(item.get("path", "")) for item in edits[:3] if isinstance(item, dict)]
        suffix = f" +{len(edits) - 3}" if isinstance(edits, list) and len(edits) > 3 else ""
        return compact(", ".join(paths) + suffix, limit=160)
    if action_name == "run_command":
        return compact(str(action.get("command", "")), limit=160)
    if action_name == "mcp_call":
        return compact(f"{action.get('server', '')}.{action.get('tool', '')}", limit=160)
    return ""


def materialize_code_response_if_needed(
    executor: ToolExecutor,
    request: str,
    response_text: str,
) -> dict[str, Any] | None:
    if not looks_like_code_artifact_request(request):
        return None
    blocks = extract_response_code_blocks(response_text)
    if not blocks:
        return None
    paths = infer_code_block_paths(request, response_text, blocks)
    traces: list[dict[str, Any]] = []
    written_paths: list[str] = []
    for block, path in zip(blocks, paths):
        trace_result = executor.write_file(path, block["code"])
        traces.append(trace_result)
        if trace_result.get("ok"):
            written_paths.append(str(trace_result.get("output", {}).get("path", path)))
    if not written_paths:
        return None
    path_lines = "\n".join(f"- {path}" for path in written_paths)
    return {
        "paths": written_paths,
        "traces": traces,
        "message": f"Wrote code to file(s):\n{path_lines}\n\nYou can ask me to modify, run, or verify these files next.",
    }


def looks_like_code_artifact_request(request: str) -> bool:
    text = request.casefold()
    terms = [
        "write code",
        "create code",
        "generate code",
        "implement",
        "script",
        "program",
        "app",
        "streamlit",
        "python",
        "javascript",
        "typescript",
        "\u5199\u4ee3\u7801",
        "\u521b\u5efa",
        "\u751f\u6210",
        "\u5b9e\u73b0",
        "\u5f00\u53d1",
        "\u811a\u672c",
        "\u7a0b\u5e8f",
        "\u7f51\u9875",
        "\u4ee3\u7801\u6587\u4ef6",
        "\u5bfc\u51fa",
        "excel",
    ]
    return any(term in text for term in terms)


def extract_response_code_blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for match in re.finditer(r"(?s)```([A-Za-z0-9_+.-]*)\s*\n(.*?)```", text):
        language = match.group(1).strip().casefold()
        code = match.group(2).strip("\n")
        if not code.strip():
            continue
        if language in {"text", "txt", "console", "terminal", "output"}:
            continue
        if looks_like_run_instruction_block(language, code):
            continue
        blocks.append({"language": language, "code": code})
    return blocks


def looks_like_run_instruction_block(language: str, code: str) -> bool:
    if language not in {"bash", "sh", "shell", "powershell", "ps1", "cmd", "bat"}:
        return False
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    if len(lines) > 4:
        return False
    run_prefixes = (
        "python ",
        "python3 ",
        "pip ",
        "npm ",
        "pnpm ",
        "yarn ",
        "streamlit ",
        "pytest",
        "uv ",
        "node ",
    )
    return all(line.casefold().startswith(run_prefixes) for line in lines)


def infer_code_block_paths(request: str, response_text: str, blocks: list[dict[str, str]]) -> list[str]:
    explicit_paths = extract_code_paths(request + "\n" + response_text)
    paths: list[str] = []
    for index, block in enumerate(blocks, start=1):
        if index <= len(explicit_paths):
            paths.append(explicit_paths[index - 1])
            continue
        extension = extension_for_code_block(block)
        base = slug_from_text(request) or "generated_code"
        suffix = "" if len(blocks) == 1 else f"_{index}"
        paths.append(f"generated/{base}{suffix}{extension}")
    return dedupe_paths(paths)


def extract_code_paths(text: str) -> list[str]:
    pattern = r"(?<![\w./\\-])([A-Za-z0-9_./\\-]+\.(?:py|js|ts|tsx|jsx|html|css|json|md|sh|ps1|sql|yaml|yml|toml|java|go|rs|cpp|c|cs))"
    paths: list[str] = []
    for match in re.finditer(pattern, text):
        path = match.group(1).replace("\\", "/").strip("./")
        if path and not path.startswith(".akernel/") and path not in paths:
            paths.append(path)
    return paths[:8]


def extension_for_code_block(block: dict[str, str]) -> str:
    language = block.get("language", "")
    code = block.get("code", "")
    mapping = {
        "python": ".py",
        "py": ".py",
        "javascript": ".js",
        "js": ".js",
        "typescript": ".ts",
        "ts": ".ts",
        "tsx": ".tsx",
        "jsx": ".jsx",
        "html": ".html",
        "css": ".css",
        "json": ".json",
        "bash": ".sh",
        "sh": ".sh",
        "powershell": ".ps1",
        "sql": ".sql",
    }
    if language in mapping:
        return mapping[language]
    if re.search(r"(?m)^\s*(import |from .* import |def |class )", code):
        return ".py"
    if "streamlit" in code.casefold() or "pandas" in code.casefold():
        return ".py"
    return ".txt"


def slug_from_text(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.casefold())
    stop = {"write", "create", "generate", "implement", "code", "script", "file", "with", "and", "the", "a", "an"}
    selected = [word for word in words if word not in stop][:6]
    return "_".join(selected)


def dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        candidate = path
        stem = str(Path(path).with_suffix(""))
        suffix = Path(path).suffix
        counter = 2
        while candidate in seen:
            candidate = f"{stem}_{counter}{suffix}"
            counter += 1
        seen.add(candidate)
        result.append(candidate)
    return result


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


def summarize_action(action: dict[str, Any] | None) -> dict[str, Any] | None:
    if not action:
        return None
    summary = {"action": action["action"]}
    for key in ["path", "command", "reason"]:
        if action.get(key):
            summary[key] = compact(str(action[key]), limit=240)
    if action["action"] == "write_file":
        summary["text"] = compact(str(action.get("text", "")), limit=120)
    if action["action"] == "patch_file":
        summary["new"] = compact(str(action.get("new", "")), limit=120)
        if action.get("start_anchor"):
            summary["start_anchor"] = compact(str(action.get("start_anchor", "")), limit=120)
            summary["end_anchor"] = compact(str(action.get("end_anchor", "")), limit=120)
            if action.get("include_anchors"):
                summary["include_anchors"] = True
        else:
            summary["old"] = compact(str(action.get("old", "")), limit=120)
            if action.get("replace_all"):
                summary["replace_all"] = True
            if action.get("occurrence") is not None:
                summary["occurrence"] = action["occurrence"]
    if action["action"] == "batch_patch":
        summary["edit_count"] = len(action.get("edits", []))
        summary["paths"] = [
            compact(str(edit.get("path", "")), limit=80)
            for edit in action.get("edits", [])[:5]
        ]
    if action["action"] == "mcp_call":
        summary["server"] = compact(str(action.get("server", "")), limit=80)
        summary["tool"] = compact(str(action.get("tool", "")), limit=80)
        summary["argument_keys"] = sorted(str(key) for key in action.get("arguments", {}).keys())[:12]
    if action["action"] == "respond":
        summary["message"] = compact(str(action.get("message", "")), limit=240)
    return summary


def summarize_tool_result(result: dict[str, Any]) -> str:
    if result["blocked"]:
        return f"blocked by policy; subject={compact(str(result['policy'].get('subject', '')), limit=180)}"
    if result.get("error"):
        return compact(str(result["error"]), limit=240)
    output = result.get("output", {})
    if result["tool"] == "read_file":
        text = compact(str(output.get("content", "")), limit=240)
        return text or "file read completed"
    if result["tool"] == "write_file":
        return f"path={compact(str(output.get('path', '')), limit=160)}; written_chars={output.get('written_chars')}"
    if result["tool"] == "patch_file":
        return (
            f"path={compact(str(output.get('path', '')), limit=160)}; "
            f"mode={output.get('mode')}; replacement_count={output.get('replacement_count')}; "
            f"delta_chars={output.get('delta_chars')}"
        )
    if result["tool"] == "batch_patch":
        return (
            f"applied_count={output.get('applied_count')}; "
            f"rolled_back={output.get('rolled_back')}; "
            f"edits={len(output.get('results', []))}"
        )
    if result["tool"] == "run_command":
        command = compact(str(output.get("command", "")), limit=160)
        stdout = compact(str(output.get("stdout", "")), limit=180)
        stderr = compact(str(output.get("stderr", "")), limit=120)
        exit_code = output.get("exit_code")
        prefix = f"command={command}; exit_code={exit_code}" if command else f"exit_code={exit_code}"
        if stdout:
            return f"{prefix}; stdout={stdout}"
        if stderr:
            return f"{prefix}; stderr={stderr}"
        timeout_seconds = output.get("timeout_seconds")
        if timeout_seconds:
            return f"{prefix}; timeout_seconds={timeout_seconds}"
        return prefix
    if result["tool"] == "mcp_call":
        server = output.get("server", "")
        tool = output.get("tool", "")
        mcp_result = output.get("result", {})
        text_parts = [
            str(item.get("text", ""))
            for item in mcp_result.get("content", [])
            if isinstance(item, dict) and item.get("text")
        ] if isinstance(mcp_result, dict) else []
        text = compact(" ".join(text_parts), limit=180)
        if text:
            return f"{server}.{tool}: {text}"
        return f"{server}.{tool}: call completed"
    return compact(str(output), limit=240)


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


def repeated_agent_action(report_steps: list[dict[str, Any]], action: dict[str, Any]) -> bool:
    if not report_steps:
        return False
    fingerprint = action_fingerprint(action)
    if not fingerprint:
        return False
    repeats = 1
    for step in reversed(report_steps):
        latest_action = step.get("action") or {}
        if action_fingerprint(latest_action) != fingerprint:
            break
        repeats += 1
    return repeats >= 3


def action_fingerprint(action: dict[str, Any]) -> str:
    action_name = action.get("action")
    if action_name == "read_file":
        return f"read_file:{action.get('path', '')}"
    if action_name == "write_file":
        return f"write_file:{action.get('path', '')}:{compact(str(action.get('text', '')), limit=80)}"
    if action_name == "patch_file":
        if action.get("start_anchor"):
            return (
                f"patch_file:{action.get('path', '')}:"
                f"anchor={compact(str(action.get('start_anchor', '')), limit=40)}:"
                f"{compact(str(action.get('end_anchor', '')), limit=40)}:"
                f"include={bool(action.get('include_anchors', False))}:"
                f"{compact(str(action.get('new', '')), limit=60)}"
            )
        return (
            f"patch_file:{action.get('path', '')}:"
            f"{compact(str(action.get('old', '')), limit=60)}:"
            f"{compact(str(action.get('new', '')), limit=60)}:"
            f"all={bool(action.get('replace_all', False))}:"
            f"occ={action.get('occurrence')}"
        )
    if action_name == "batch_patch":
        pieces = []
        for edit in action.get("edits", [])[:8]:
            pieces.append(
                f"{edit.get('path', '')}:"
                f"{compact(str(edit.get('old') or edit.get('start_anchor') or ''), limit=30)}:"
                f"{compact(str(edit.get('new', '')), limit=30)}"
            )
        return "batch_patch:" + "|".join(pieces)
    if action_name == "run_command":
        return f"run_command:{action.get('command', '')}"
    return ""


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


def require_non_empty_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def optional_non_empty_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string when provided")
    return value.strip()


def clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def optional_int(value: Any, *, minimum: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"value must be at least {minimum}")
    return parsed


def compact(text: str, limit: int = 300) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
