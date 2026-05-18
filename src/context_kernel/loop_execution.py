from __future__ import annotations

from typing import Any

from .loop_actions import compact, parse_agent_action, repeated_agent_action, summarize_action
from .loop_recovery import diagnose_agent_exception
from .loop_steps import agent_step_result
from .runner import AgentRunner
from .storage import Workspace
from .tasks import TaskStore
from .tools import MAX_CAPTURE_CHARS


def response_token_counts(trace: dict[str, Any]) -> dict[str, int]:
    response = trace.get("response", {})
    return {
        "input_tokens": int(response.get("input_tokens", 0) or 0),
        "output_tokens": int(response.get("output_tokens", 0) or 0),
        "total_tokens": int(response.get("total_tokens", 0) or 0),
    }


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
