from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .memory import MemoryStore
from .models import utc_now
from .planner import ExecutionPlanner
from .providers import extract_anchor_patch_instruction, extract_patch_instruction, extract_write_instruction
from .runner import AgentRunner
from .skills import extract_json_object
from .storage import Workspace
from .tasks import TaskStore
from .tools import MAX_CAPTURE_CHARS, ToolExecutor


TOOL_ACTIONS = {"read_file", "write_file", "patch_file", "batch_patch", "run_command"}
ALLOWED_ACTIONS = TOOL_ACTIONS | {"respond"}


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
        base_url: str | None = None,
        task_id: str | None = None,
        max_steps: int = 5,
        remember: bool = True,
        allow_over_budget: bool = False,
        expect_json: bool = False,
    ) -> dict[str, Any]:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")

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
            "state": {"enabled": False, "candidate_count": 0, "written_count": 0, "records": []},
            "totals": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }

        for index in range(1, max_steps + 1):
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
                base_url=base_url,
                allow_over_budget=allow_over_budget,
                expect_json=expect_json,
            )
            report["steps"].append(step)
            add_tokens(report["totals"], step.get("tokens", {}))
            if step.get("final_response") is not None:
                report["final_response"] = step["final_response"]

            if not step["continue"]:
                report["status"] = step["status"]
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
        base_url: str | None,
        allow_over_budget: bool,
        expect_json: bool,
    ) -> dict[str, Any]:
        plan = ExecutionPlanner(self.workspace).plan(
            request,
            budget,
            profile,
            task_id=task_id,
            resume=True,
        )
        if plan["budget"]["over_budget"] and not allow_over_budget:
            return {
                "index": index,
                "status": "blocked",
                "continue": False,
                "stop_reason": "Agent loop stopped: context packet is over budget.",
                "reason": "context packet is over budget",
                "plan": summarize_plan(plan),
                "trace_id": None,
                "tool_trace_id": None,
                "action": None,
                "tokens": {},
                "verifier_ok": False,
            }

        trace = AgentRunner(self.workspace).run(
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
            packet_overrides=build_agent_packet(request, index, max_steps, expect_json=expect_json),
        )
        attach_trace_outputs(self.tasks, task_id, trace)
        tokens = trace.get("response", {})

        if not trace.get("verifier", {}).get("ok"):
            self.tasks.step(
                task_id,
                f"Agent step {index} needs review: provider did not satisfy the JSON action contract.",
                kind="agent_step",
                refs={"run_traces": [trace["id"]]},
            )
            return {
                "index": index,
                "status": "needs_review",
                "continue": False,
                "stop_reason": "Agent loop stopped: provider response failed JSON action verification.",
                "plan": summarize_plan(plan),
                "trace_id": trace["id"],
                "tool_trace_id": None,
                "action": None,
                "tokens": {
                    "input_tokens": tokens.get("input_tokens", 0),
                    "output_tokens": tokens.get("output_tokens", 0),
                    "total_tokens": tokens.get("total_tokens", 0),
                },
                "verifier_ok": False,
            }

        try:
            action = parse_agent_action(trace["response"]["text"], expect_json=expect_json)
        except (ValueError, KeyError, TypeError) as exc:
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
                "stop_reason": "Agent loop stopped: provider returned an invalid action payload.",
                "reason": str(exc),
                "plan": summarize_plan(plan),
                "trace_id": trace["id"],
                "tool_trace_id": None,
                "action": None,
                "tokens": {
                    "input_tokens": tokens.get("input_tokens", 0),
                    "output_tokens": tokens.get("output_tokens", 0),
                    "total_tokens": tokens.get("total_tokens", 0),
                },
                "verifier_ok": True,
            }

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
                "stop_reason": "Agent loop stopped: repeated identical action would likely cause a loop.",
                "reason": "repeated identical action detected",
                "plan": summarize_plan(plan),
                "trace_id": trace["id"],
                "tool_trace_id": None,
                "action": summarize_action(action),
                "tokens": {
                    "input_tokens": tokens.get("input_tokens", 0),
                    "output_tokens": tokens.get("output_tokens", 0),
                    "total_tokens": tokens.get("total_tokens", 0),
                },
                "verifier_ok": True,
            }

        if action["action"] == "respond":
            response_text = render_agent_response(action)
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
                "tool_trace_id": None,
                "action": summarize_action(action),
                "tokens": {
                    "input_tokens": tokens.get("input_tokens", 0),
                    "output_tokens": tokens.get("output_tokens", 0),
                    "total_tokens": tokens.get("total_tokens", 0),
                },
                "verifier_ok": True,
                "final_response": response_text,
            }

        tool_result = execute_agent_action(self.tools, action)
        self.tasks.attach(task_id, "tool", tool_result["id"])
        tool_summary = summarize_tool_result(tool_result)
        self.tasks.step(
            task_id,
            f"Agent step {index} executed {action['action']}: {tool_summary}",
            kind="agent_tool",
            refs={"run_traces": [trace["id"]], "tool_traces": [tool_result["id"]]},
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
        can_continue = index < max_steps and (tool_result["ok"] or bool(recovery_tools))
        if tool_result["blocked"]:
            status = "blocked"
        elif not tool_result["ok"]:
            status = "recovery_prepared" if can_continue else "needs_review"
        else:
            status = "ok" if can_continue else "stopped"
        return {
            "index": index,
            "status": status,
            "continue": can_continue,
            "stop_reason": final_tool_stop_reason(tool_result, recovery_tools=recovery_tools, max_steps=max_steps) if not can_continue else "",
            "plan": summarize_plan(plan),
            "trace_id": trace["id"],
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
            "verifier_ok": True,
        }


def build_agent_packet(request: str, step_index: int, max_steps: int, *, expect_json: bool = False) -> dict[str, Any]:
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
        "Respect policy-gated tools; do not ask for destructive operations.",
        "Before choosing run_command, check runtime.command_policy.allowed_roots and only use an allowed command root.",
        "If the user's requested command root is outside runtime.command_policy.allowed_roots, respond with the restriction instead of retrying the blocked command.",
        "Prefer reusing task brief summaries instead of repeating a completed tool action.",
        "If a patch or verification step fails, check any recovery read summaries before deciding the next action.",
        "When the user describes a block between markers, use patch_file with start_anchor and end_anchor instead of rewriting the whole file.",
        "When the user asks for multiple file edits, prefer one batch_patch action with an edits array.",
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
            ],
            "response_contract": {
                "type": "json_object",
                "rules": rules,
            },
        }
    }


def parse_agent_action(text: str, *, expect_json: bool = False) -> dict[str, Any]:
    action = extract_json_object(text)
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
    command = require_non_empty_string(action, "command")
    timeout_seconds = clamp_int(action.get("timeout_seconds", 30), default=30, minimum=1, maximum=300)
    return {
        "action": "run_command",
        "command": command,
        "timeout_seconds": timeout_seconds,
    "reason": compact(str(action.get("reason", "")), limit=240),
    }


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
    raise ValueError(f"Unsupported tool action: {action['action']}")


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
        target = recovery_target_path(request)
        if target:
            return [executor.read_file(target, max_chars=min(4000, MAX_CAPTURE_CHARS))]
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
        "steps": [compact_saved_step(step) for step in steps],
        "final_response": compact(str(report.get("final_response") or ""), limit=800) or None,
        "state": compact_saved_state(report.get("state", {})),
        "totals": compact_saved_tokens(report.get("totals", {})),
        "storage": {
            "detail_level": "compact_v1",
            "step_count": len(steps),
            "full_details_in": {
                "run_traces": unique_non_empty([step.get("trace_id") for step in steps]),
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
        "action": step.get("action"),
        "tokens": compact_saved_tokens(step.get("tokens", {})),
        "verifier_ok": step.get("verifier_ok"),
    }
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
        stdout = compact(str(output.get("stdout", "")), limit=180)
        stderr = compact(str(output.get("stderr", "")), limit=120)
        exit_code = output.get("exit_code")
        if stdout:
            return f"exit_code={exit_code}; stdout={stdout}"
        if stderr:
            return f"exit_code={exit_code}; stderr={stderr}"
        return f"exit_code={exit_code}"
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
    latest = report_steps[-1]
    latest_action = latest.get("action") or {}
    return bool(fingerprint) and action_fingerprint(latest_action) == fingerprint


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
