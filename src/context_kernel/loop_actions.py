from __future__ import annotations

import json
from typing import Any

from .mcp import call_mcp_tool
from .skills import extract_json_object
from .tools import MAX_CAPTURE_CHARS, ToolExecutor


TOOL_ACTIONS = {
    "list_dir",
    "file_info",
    "read_file",
    "create_file",
    "write_file",
    "append_file",
    "patch_file",
    "batch_patch",
    "transaction",
    "run_command",
    "mcp_call",
}
ALLOWED_ACTIONS = TOOL_ACTIONS | {"respond"}


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
    if action_name == "list_dir":
        path = str(action.get("path") or ".").strip() or "."
        limit = clamp_int(action.get("limit", 200), default=200, minimum=1, maximum=200)
        return {
            "action": "list_dir",
            "path": path,
            "limit": limit,
            "reason": compact(str(action.get("reason", "")), limit=240),
        }
    if action_name == "file_info":
        path = require_non_empty_string(action, "path")
        return {
            "action": "file_info",
            "path": path,
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
    if action_name in {"create_file", "write_file"}:
        path = require_non_empty_string(action, "path")
        text = str(action.get("text", ""))
        return {
            "action": action_name,
            "path": path,
            "text": text,
            "reason": compact(str(action.get("reason", "")), limit=240),
        }
    if action_name == "append_file":
        path = require_non_empty_string(action, "path")
        text = str(action.get("text", ""))
        return {
            "action": "append_file",
            "path": path,
            "text": text,
            "create": bool(action.get("create", True)),
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
    if action_name == "transaction":
        steps = action.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("transaction requires a non-empty steps array")
        return {
            "action": "transaction",
            "steps": [parse_transaction_step(step) for step in steps],
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
    if any(payload.get(key) for key in ["action", "tool", "name", "tool_name"]):
        return payload
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
        "ls": "list_dir",
        "list": "list_dir",
        "list_files": "list_dir",
        "list_directory": "list_dir",
        "stat": "file_info",
        "file_stat": "file_info",
        "inspect_file": "file_info",
        "create": "create_file",
        "create_file": "create_file",
        "write": "write_file",
        "file_write": "write_file",
        "append": "append_file",
        "append_file": "append_file",
        "patch": "patch_file",
        "edit_file": "patch_file",
        "batch_edit": "batch_patch",
        "transact": "transaction",
        "atomic_transaction": "transaction",
        "transactional_edit": "transaction",
        "tool_transaction": "transaction",
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


def parse_transaction_step(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ValueError("transaction steps must be objects")
    action_name = normalize_action_name(str(step.get("action") or step.get("tool") or step.get("name") or ""))
    if action_name in {"create_file", "write_file"}:
        return {
            "action": action_name,
            "path": require_non_empty_string(step, "path"),
            "text": str(step.get("text", "")),
        }
    if action_name == "append_file":
        return {
            "action": "append_file",
            "path": require_non_empty_string(step, "path"),
            "text": str(step.get("text", "")),
            "create": bool(step.get("create", True)),
        }
    if action_name == "patch_file":
        return {"action": "patch_file", **parse_patch_edit(step)}
    if action_name == "run_command":
        return {
            "action": "run_command",
            "command": require_non_empty_string(step, "command"),
            "timeout_seconds": clamp_int(step.get("timeout_seconds", step.get("timeout", 30)), default=30, minimum=1, maximum=300),
        }
    raise ValueError(f"transaction step has unsupported action: {action_name or '[missing]'}")


def execute_agent_action(executor: ToolExecutor, action: dict[str, Any]) -> dict[str, Any]:
    if action["action"] == "list_dir":
        return executor.list_dir(action.get("path", "."), limit=action.get("limit", 200))
    if action["action"] == "file_info":
        return executor.file_info(action["path"])
    if action["action"] == "read_file":
        return executor.read_file(action["path"], max_chars=action["max_chars"])
    if action["action"] == "create_file":
        return executor.create_file(action["path"], action["text"])
    if action["action"] == "write_file":
        return executor.write_file(action["path"], action["text"])
    if action["action"] == "append_file":
        return executor.append_file(action["path"], action["text"], create=bool(action.get("create", True)))
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
    if action["action"] == "transaction":
        return executor.transaction(action["steps"])
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
    if action_name == "list_dir":
        return "listing directory"
    if action_name == "file_info":
        return "checking file"
    if action_name == "read_file":
        return "reading file"
    if action_name == "create_file":
        return "creating file"
    if action_name == "write_file":
        return "creating or updating file"
    if action_name == "append_file":
        return "appending file"
    if action_name == "patch_file":
        return "applying file patch"
    if action_name == "batch_patch":
        return "applying multi-file patch"
    if action_name == "transaction":
        return "running transaction"
    if action_name == "run_command":
        return "running command"
    if action_name == "mcp_call":
        return "calling MCP tool"
    return action_name or "running action"


def action_progress_target(action: dict[str, Any]) -> str:
    action_name = str(action.get("action", ""))
    if action_name in {"list_dir", "file_info", "read_file", "create_file", "write_file", "append_file", "patch_file"}:
        return compact(str(action.get("path", "")), limit=160)
    if action_name == "batch_patch":
        edits = action.get("edits", [])
        paths = [str(item.get("path", "")) for item in edits[:3] if isinstance(item, dict)]
        suffix = f" +{len(edits) - 3}" if isinstance(edits, list) and len(edits) > 3 else ""
        return compact(", ".join(paths) + suffix, limit=160)
    if action_name == "transaction":
        steps = action.get("steps", [])
        names = [str(item.get("action", "")) for item in steps[:4] if isinstance(item, dict)]
        suffix = f" +{len(steps) - 4}" if isinstance(steps, list) and len(steps) > 4 else ""
        return compact(", ".join(names) + suffix, limit=160)
    if action_name == "run_command":
        return compact(str(action.get("command", "")), limit=160)
    if action_name == "mcp_call":
        return compact(f"{action.get('server', '')}.{action.get('tool', '')}", limit=160)
    return ""


def summarize_action(action: dict[str, Any] | None) -> dict[str, Any] | None:
    if not action:
        return None
    summary = {"action": action["action"]}
    for key in ["path", "command", "reason"]:
        if action.get(key):
            summary[key] = compact(str(action[key]), limit=240)
    if action["action"] in {"create_file", "write_file", "append_file"}:
        summary["text"] = compact(str(action.get("text", "")), limit=120)
    if action["action"] == "append_file":
        summary["create"] = bool(action.get("create", True))
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
    if action["action"] == "transaction":
        steps = action.get("steps", [])
        summary["step_count"] = len(steps)
        summary["steps"] = [
            compact(str(step.get("action", "")), limit=80)
            for step in steps[:6]
            if isinstance(step, dict)
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
    output = result.get("output", {})
    if result["tool"] == "transaction":
        summary = (
            f"applied_count={output.get('applied_count')}; "
            f"rolled_back={output.get('rolled_back')}; "
            f"steps={len(output.get('results', []))}"
        )
        if result.get("error"):
            return f"{compact(str(result['error']), limit=180)}; {summary}"
        return summary
    if result.get("error"):
        return compact(str(result["error"]), limit=240)
    if result["tool"] == "list_dir":
        names = ", ".join(str(item.get("name", "")) for item in output.get("entries", [])[:8])
        suffix = "..." if output.get("truncated") else ""
        return f"path={compact(str(output.get('path', '')), limit=120)}; entries={output.get('total_entries', 0)}; {compact(names + suffix, limit=160)}"
    if result["tool"] == "file_info":
        return (
            f"path={compact(str(output.get('path', '')), limit=160)}; "
            f"exists={output.get('exists')}; kind={output.get('kind', 'missing')}; "
            f"size={output.get('size_bytes')}"
        )
    if result["tool"] == "read_file":
        text = compact(str(output.get("content", "")), limit=240)
        return text or "file read completed"
    if result["tool"] in {"create_file", "write_file"}:
        return (
            f"path={compact(str(output.get('path', '')), limit=160)}; "
            f"written_chars={output.get('written_chars')}; "
            f"created={output.get('created')}; overwritten={output.get('overwritten')}"
        )
    if result["tool"] == "append_file":
        return (
            f"path={compact(str(output.get('path', '')), limit=160)}; "
            f"appended_chars={output.get('appended_chars')}; created={output.get('created')}"
        )
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
    if action_name == "list_dir":
        return f"list_dir:{action.get('path', '')}:{action.get('limit', '')}"
    if action_name == "file_info":
        return f"file_info:{action.get('path', '')}"
    if action_name == "read_file":
        return f"read_file:{action.get('path', '')}"
    if action_name in {"create_file", "write_file", "append_file"}:
        return f"{action_name}:{action.get('path', '')}:{compact(str(action.get('text', '')), limit=80)}"
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
    if action_name == "transaction":
        pieces = []
        for step in action.get("steps", [])[:8]:
            pieces.append(
                f"{step.get('action', '')}:"
                f"{step.get('path', step.get('command', ''))}:"
                f"{compact(str(step.get('text') or step.get('new') or ''), limit=30)}"
            )
        return "transaction:" + "|".join(pieces)
    if action_name == "run_command":
        return f"run_command:{action.get('command', '')}"
    return ""


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
