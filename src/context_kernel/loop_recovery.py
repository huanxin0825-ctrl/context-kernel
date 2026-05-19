from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .loop_actions import compact, summarize_tool_result
from .providers import extract_anchor_patch_instruction, extract_patch_instruction, extract_write_instruction
from .storage import Workspace
from .tools import MAX_CAPTURE_CHARS, ToolExecutor


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
    if tool == "transaction":
        return {
            "category": "transaction_failed",
            "message": summary,
            "suggestion": "The transaction was rolled back. Inspect the linked trace, fix the failing step, then rerun the task.",
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


def final_tool_stop_reason(result: dict[str, Any], *, recovery_tools: list[dict[str, Any]], max_steps: int) -> str:
    if result["blocked"]:
        return "Agent loop stopped: the final tool action was blocked by policy."
    if not result["ok"]:
        if recovery_tools:
            return "Agent loop stopped: recovery context was prepared, but no loop step remained to use it."
        return "Agent loop stopped: the final tool action failed and needs review."
    return f"Agent loop stopped after {max_steps} step(s)."
