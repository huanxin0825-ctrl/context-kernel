from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .policy import command_root_candidates
from .tokenizer import estimate_tokens


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int


class ModelProvider(Protocol):
    name: str

    def run(self, packet: dict[str, Any]) -> ProviderResponse:
        ...


class MockProvider:
    name = "mock"

    def run(self, packet: dict[str, Any]) -> ProviderResponse:
        if str(packet.get("agent", {}).get("mode", "")) == "aux_review_v1":
            return self._run_aux_review(packet)
        if str(packet.get("agent", {}).get("mode", "")).startswith("tool_planning_v"):
            return self._run_tool_planning(packet)
        skill_names = [item["contract"]["name"] for item in packet.get("skills", [])]
        memory_count = len(packet.get("memory", []))
        text = (
            "Mock provider received a minimal context packet.\n"
            f"Request: {packet['request']}\n"
            f"Selected skills: {', '.join(skill_names) if skill_names else 'none'}\n"
            f"Selected memories: {memory_count}\n"
            f"Estimated input tokens: {packet['budget']['estimated_used']}"
        )
        return ProviderResponse(
            text=text,
            input_tokens=estimate_tokens(packet),
            output_tokens=estimate_tokens(text),
        )

    def _run_aux_review(self, packet: dict[str, Any]) -> ProviderResponse:
        budget = packet.get("budget", {})
        review = packet.get("agent", {}).get("review", {})
        warnings = review.get("warnings", [])
        risk = "high" if budget.get("over_budget") else "medium" if warnings else "low"
        recommendation = "reduce_context" if budget.get("over_budget") else "continue"
        payload = {
            "ok": not bool(budget.get("over_budget")),
            "risk": risk,
            "recommendation": recommendation,
            "notes": [
                f"route={review.get('route_mode', 'unknown')}",
                f"complexity={review.get('complexity', 'unknown')}",
                f"estimated_tokens={budget.get('estimated_used', 0)}",
            ],
        }
        text = json.dumps(payload, ensure_ascii=False)
        return ProviderResponse(
            text=text,
            input_tokens=estimate_tokens(packet),
            output_tokens=estimate_tokens(text),
        )

    def _run_tool_planning(self, packet: dict[str, Any]) -> ProviderResponse:
        request = str(packet.get("request", ""))
        linked_tools = packet.get("task", {}).get("brief", {}).get("linked_tool_traces", [])
        allowed_roots = set(packet.get("runtime", {}).get("command_policy", {}).get("allowed_roots", []))
        project = packet.get("runtime", {}).get("project")
        project_commands = project.get("commands", {}) if isinstance(project, dict) else {}
        payload = (
            self._mock_batch_patch_verify_action(request, linked_tools, allowed_roots, project_commands)
            or self._mock_patch_verify_action(request, linked_tools, allowed_roots, project_commands)
            or self._mock_write_verify_action(request, linked_tools, allowed_roots, project_commands)
            or self._mock_read_file_action(request, linked_tools)
            or self._mock_fix_failing_tests_action(request, linked_tools, allowed_roots, project_commands)
            or self._mock_run_command_action(request, linked_tools, allowed_roots, project_commands)
            or self._mock_write_file_action(request, linked_tools)
            or self._mock_batch_patch_action(request, linked_tools)
            or self._mock_patch_file_action(request, linked_tools)
            or {
                "action": "respond",
                "message": f"Mock agent response for request: {request}",
                "reason": "No tool action was needed.",
            }
        )
        text = json.dumps(payload, ensure_ascii=False)
        return ProviderResponse(
            text=text,
            input_tokens=estimate_tokens(packet),
            output_tokens=estimate_tokens(text),
        )

    def _mock_batch_patch_verify_action(
        self,
        request: str,
        linked_tools: list[dict[str, Any]],
        allowed_roots: set[str],
        project_commands: dict[str, str],
    ) -> dict[str, Any] | None:
        edits = extract_batch_patch_requests(request)
        command = resolve_requested_command(request, project_commands)
        if len(edits) < 2 or not command:
            return None
        prior = find_tool_trace(linked_tools, "batch_patch", str(edits[0]["path"]))
        command_trace = find_command_trace(linked_tools, command)
        if not prior:
            return {
                "action": "batch_patch",
                "edits": edits,
                "reason": "Apply the requested multi-file patch batch before verification.",
            }
        summary = prior.get("output_summary") or "batch patch completed"
        if prior.get("blocked") or not prior.get("ok", False):
            return {
                "action": "respond",
                "message": f"Batch patch could not be completed: {summary}",
                "reason": "Stop because the batch patch did not succeed.",
            }
        if not command_trace:
            return run_command_or_policy_response(
                command,
                allowed_roots,
                reason="Run the requested verification command after the batch patch.",
            )
        command_summary = command_trace.get("output_summary") or "command completed"
        if command_trace.get("blocked") or not command_trace.get("ok", False):
            return {
                "action": "respond",
                "message": f"Batch patch completed: {summary}. Verification command `{command}` failed: {command_summary}",
                "reason": "Stop after surfacing the verification result.",
            }
        return {
            "action": "respond",
            "message": f"Batch patch completed: {summary}. Verification command `{command}` succeeded: {command_summary}",
            "reason": "The batch patch and verification command have completed.",
        }

    def _mock_patch_verify_action(
        self,
        request: str,
        linked_tools: list[dict[str, Any]],
        allowed_roots: set[str],
        project_commands: dict[str, str],
    ) -> dict[str, Any] | None:
        patch = extract_patch_request(request)
        command = resolve_requested_command(request, project_commands)
        if not patch or not command:
            return None
        path = str(patch["path"])
        patch_trace = find_tool_trace(linked_tools, "patch_file", path)
        write_trace = find_tool_trace(linked_tools, "write_file", path)
        read_trace = find_tool_trace(linked_tools, "read_file", path)
        command_trace = find_command_trace(linked_tools, command)

        if not patch_trace:
            return patch_action_from_request(patch, reason="Apply the requested patch before verification.")

        if patch_trace.get("blocked"):
            patch_summary = patch_trace.get("output_summary") or "patch was blocked"
            return {
                "action": "respond",
                "message": f"Patch for {path} could not be applied: {patch_summary}",
                "reason": "Stop because the requested patch was blocked.",
            }

        patch_summary = patch_trace.get("output_summary") or "patch completed"
        if not patch_trace.get("ok", False):
            return self._recover_patch_failure(
                patch,
                command,
                patch_summary,
                read_trace,
                write_trace,
                command_trace,
                allowed_roots,
            )

        if not command_trace:
            return run_command_or_policy_response(
                command,
                allowed_roots,
                reason="Run the requested verification command after the patch.",
            )
        command_summary = command_trace.get("output_summary") or "command completed"
        if command_trace.get("blocked"):
            return {
                "action": "respond",
                "message": (
                    f"Patched {path}: {patch_summary}. "
                    f"Verification command `{command}` failed: {command_summary}"
                ),
                "reason": "Stop after surfacing the blocked verification result.",
            }
        if not command_trace.get("ok", False):
            return self._recover_verification_failure(
                path=path,
                desired_text=str(patch["new"]),
                command=command,
                success_summary=patch_summary,
                failure_summary=command_summary,
                read_trace=read_trace,
                write_trace=write_trace,
                allowed_roots=allowed_roots,
            )
        return {
            "action": "respond",
            "message": (
                f"Patched {path}: {patch_summary}. "
                f"Verification command `{command}` succeeded: {command_summary}"
            ),
            "reason": "Both the patch and verification command have completed.",
        }

    def _mock_write_verify_action(
        self,
        request: str,
        linked_tools: list[dict[str, Any]],
        allowed_roots: set[str],
        project_commands: dict[str, str],
    ) -> dict[str, Any] | None:
        write = extract_write_instruction(request)
        command = resolve_requested_command(request, project_commands)
        if not write or not command:
            return None
        path, text = write
        write_trace = find_tool_trace(linked_tools, "write_file", path)
        read_trace = find_tool_trace(linked_tools, "read_file", path)
        command_trace = find_command_trace(linked_tools, command)

        if not write_trace:
            return {
                "action": "write_file",
                "path": path,
                "text": text,
                "reason": "Create the requested file before verification.",
            }
        write_summary = write_trace.get("output_summary") or "write completed"
        if write_trace.get("blocked"):
            return {
                "action": "respond",
                "message": f"File {path} could not be written: {write_summary}",
                "reason": "Stop because the requested file write was blocked.",
            }
        if not write_trace.get("ok", False):
            if read_trace:
                recovered_text = read_content(read_trace.get("output_summary", ""))
                if recovered_text != text:
                    return {
                        "action": "write_file",
                        "path": path,
                        "text": text,
                        "reason": "Retry the requested file write after inspecting the current contents.",
                    }
            return {
                "action": "respond",
                "message": f"File {path} could not be written: {write_summary}",
                "reason": "Stop because the requested file write did not succeed.",
            }

        if not command_trace:
            return run_command_or_policy_response(
                command,
                allowed_roots,
                reason="Run the requested verification command after writing the file.",
            )
        command_summary = command_trace.get("output_summary") or "command completed"
        if command_trace.get("blocked"):
            return {
                "action": "respond",
                "message": (
                    f"Wrote {path}: {write_summary}. "
                    f"Verification command `{command}` failed: {command_summary}"
                ),
                "reason": "Stop after surfacing the blocked verification result.",
            }
        if not command_trace.get("ok", False):
            return self._recover_verification_failure(
                path=path,
                desired_text=text,
                command=command,
                success_summary=write_summary,
                failure_summary=command_summary,
                read_trace=read_trace,
                write_trace=write_trace,
                allowed_roots=allowed_roots,
            )
        return {
            "action": "respond",
            "message": (
                f"Wrote {path}: {write_summary}. "
                f"Verification command `{command}` succeeded: {command_summary}"
            ),
            "reason": "Both the file write and verification command have completed.",
        }

    def _recover_patch_failure(
        self,
        patch: dict[str, Any],
        command: str,
        patch_summary: str,
        read_trace: dict[str, Any] | None,
        write_trace: dict[str, Any] | None,
        command_trace: dict[str, Any] | None,
        allowed_roots: set[str],
    ) -> dict[str, Any]:
        path = str(patch["path"])
        if not read_trace:
            return {
                "action": "read_file",
                "path": path,
                "max_chars": 4000,
                "reason": "Inspect the file after the patch failure so we can rewrite it safely.",
            }
        summary = read_trace.get("output_summary", "")
        current_text = read_content(summary)
        if patch.get("start_anchor"):
            rewritten = rewrite_anchor_block_from_summary(
                summary,
                str(patch.get("start_anchor", "")),
                str(patch.get("end_anchor", "")),
                str(patch["new"]),
                include_anchors=bool(patch.get("include_anchors", False)),
            )
        else:
            old = str(patch.get("old", ""))
            matches = current_text.count(old)
            if matches > 1 and not patch.get("replace_all"):
                retry_patch = dict(patch)
                retry_patch["replace_all"] = True
                retry_patch["occurrence"] = None
                return patch_action_from_request(
                    retry_patch,
                    reason="Recover from the failed single-match patch by replacing all inspected matches.",
                )
            rewritten = rewrite_text_from_summary(summary, old, str(patch["new"]))
        if rewritten is None:
            return {
                "action": "respond",
                "message": f"Patch for {path} could not be applied: {patch_summary}",
                "reason": "Stop because the file contents do not support a safe rewrite recovery.",
            }
        if not write_trace:
            return {
                "action": "write_file",
                "path": path,
                "text": rewritten,
                "reason": "Recover from the failed patch by rewriting the file from inspected contents.",
            }
        write_summary = write_trace.get("output_summary") or "write completed"
        if write_trace.get("blocked") or not write_trace.get("ok", False):
            return {
                "action": "respond",
                "message": f"Patch recovery for {path} failed: {write_summary}",
                "reason": "Stop because the rewrite recovery did not succeed.",
            }
        if not command_trace:
            return run_command_or_policy_response(
                command,
                allowed_roots,
                reason="Run the requested verification command after rewrite recovery.",
            )
        command_summary = command_trace.get("output_summary") or "command completed"
        if command_trace.get("blocked") or not command_trace.get("ok", False):
            return self._recover_verification_failure(
                path=path,
                desired_text=str(patch["new"]),
                command=command,
                success_summary=write_summary,
                failure_summary=command_summary,
                read_trace=read_trace,
                write_trace=write_trace,
                allowed_roots=allowed_roots,
            )
        return {
            "action": "respond",
            "message": (
                f"Recovered {path} with a rewrite: {write_summary}. "
                f"Verification command `{command}` succeeded: {command_summary}"
            ),
            "reason": "Patch recovery and verification have completed successfully.",
        }

    def _recover_verification_failure(
        self,
        *,
        path: str,
        desired_text: str,
        command: str,
        success_summary: str,
        failure_summary: str,
        read_trace: dict[str, Any] | None,
        write_trace: dict[str, Any] | None,
        allowed_roots: set[str],
    ) -> dict[str, Any]:
        if not read_trace:
            return {
                "action": "read_file",
                "path": path,
                "max_chars": 4000,
                "reason": "Inspect the file after verification failed before attempting another edit.",
            }
        current_text = read_content(read_trace.get("output_summary", ""))
        if desired_text not in current_text:
            if not write_trace or desired_text not in str(write_trace.get("output_summary", "")):
                return {
                    "action": "write_file",
                    "path": path,
                    "text": rewrite_to_include_text(current_text, desired_text),
                    "reason": "Retry the edit because verification failed and the current file still lacks the expected text.",
                }
        return {
            "action": "respond",
            "message": (
                f"Updated {path}: {success_summary}. "
                f"Verification command `{command}` still failed: {failure_summary}"
            ),
            "reason": "Stop because the file appears updated and the remaining failure is likely outside the edit itself.",
        }

    def _mock_read_file_action(self, request: str, linked_tools: list[dict[str, Any]]) -> dict[str, Any] | None:
        match = re.search(r"(?i)\bread\s+([^\s,;]+)", request)
        if not match:
            return None
        path = match.group(1).strip().strip("'\"")
        prior = find_tool_trace(linked_tools, "read_file", path)
        if prior:
            summary = prior.get("output_summary") or "file was read"
            return {
                "action": "respond",
                "message": f"Read {path}: {summary}",
                "reason": "The requested file content is already available in task state.",
            }
        return {
            "action": "read_file",
            "path": path,
            "max_chars": 2000,
            "reason": "Need the file contents before responding.",
        }

    def _mock_run_command_action(
        self,
        request: str,
        linked_tools: list[dict[str, Any]],
        allowed_roots: set[str],
        project_commands: dict[str, str],
    ) -> dict[str, Any] | None:
        command = resolve_requested_command(request, project_commands)
        if not command:
            return None
        prior = find_command_trace(linked_tools, command)
        if prior:
            summary = prior.get("output_summary") or "command completed"
            return {
                "action": "respond",
                "message": f"Command `{command}` result: {summary}",
                "reason": "The command output is already available in task state.",
            }
        return run_command_or_policy_response(
            command,
            allowed_roots,
            reason="Need command output before responding.",
        )

    def _mock_fix_failing_tests_action(
        self,
        request: str,
        linked_tools: list[dict[str, Any]],
        allowed_roots: set[str],
        project_commands: dict[str, str],
    ) -> dict[str, Any] | None:
        if not asks_to_fix_tests(request):
            return None
        command = resolve_requested_command(request, project_commands)
        if not command:
            return None
        command_traces = find_command_traces(linked_tools, command)
        if not command_traces:
            return run_command_or_policy_response(
                command,
                allowed_roots,
                reason="Run the project test command before attempting a fix.",
            )

        latest_command = command_traces[-1]
        command_summary = latest_command.get("output_summary") or "command completed"
        if latest_command.get("ok") and not latest_command.get("blocked"):
            return {
                "action": "respond",
                "message": f"Project tests passed: {command_summary}",
                "reason": "The project test command now succeeds.",
            }
        if latest_command.get("blocked"):
            return {
                "action": "respond",
                "message": f"Project test command `{command}` was blocked: {command_summary}",
                "reason": "Stop because verification is blocked by policy.",
            }

        failure_paths = failure_paths_from_summary(command_summary)
        failure_path = failure_paths[0] if failure_paths else None
        read_traces = [trace for path in failure_paths if (trace := find_tool_trace(linked_tools, "read_file", path))]
        unread_path = next((path for path in failure_paths if not find_tool_trace(linked_tools, "read_file", path)), None)
        if unread_path:
            return {
                "action": "read_file",
                "path": unread_path,
                "max_chars": 4000,
                "reason": "Read the file referenced by the failing test output before patching.",
            }

        patch_trace = find_tool_trace(linked_tools, "patch_file", failure_path) if failure_path else None
        batch_patch_trace = find_tool_trace(linked_tools, "batch_patch", failure_path) if failure_path else None
        if (patch_trace or batch_patch_trace) and len(command_traces) == 1:
            return run_command_or_policy_response(
                command,
                allowed_roots,
                reason="Re-run the project test command after applying the fix.",
            )
        if (patch_trace or batch_patch_trace) and len(command_traces) > 1:
            return {
                "action": "respond",
                "message": f"Applied a candidate fix, but `{command}` still failed: {command_summary}",
                "reason": "Stop after one verified fix attempt to avoid looping.",
            }

        if read_traces:
            multi_patch = infer_batch_patch_from_failures(read_traces, command_summary)
            if multi_patch:
                return multi_patch
            patch = infer_patch_from_failure(read_traces[0], command_summary)
            if patch:
                return patch

        return {
            "action": "respond",
            "message": f"Project tests failed, but no safe automatic patch was inferred: {command_summary}",
            "reason": "Stop because the failing output did not map to a safe patch.",
        }

    def _mock_batch_patch_action(self, request: str, linked_tools: list[dict[str, Any]]) -> dict[str, Any] | None:
        edits = extract_batch_patch_requests(request)
        if len(edits) < 2:
            return None
        prior = find_tool_trace(linked_tools, "batch_patch", str(edits[0]["path"]))
        if prior:
            summary = prior.get("output_summary") or "batch patch was applied"
            return {
                "action": "respond",
                "message": f"Batch patch result: {summary}",
                "reason": "The requested batch patch is already recorded in task state.",
            }
        return {
            "action": "batch_patch",
            "edits": edits,
            "reason": "Apply the requested multi-file patch batch before responding.",
        }

    def _mock_patch_file_action(self, request: str, linked_tools: list[dict[str, Any]]) -> dict[str, Any] | None:
        patch = extract_patch_request(request)
        if not patch:
            return None
        path = str(patch["path"])
        prior = find_tool_trace(linked_tools, "patch_file", path)
        read_trace = find_tool_trace(linked_tools, "read_file", path)
        write_trace = find_tool_trace(linked_tools, "write_file", path)
        if prior:
            summary = prior.get("output_summary") or "patch was applied"
            if prior.get("blocked"):
                return {
                    "action": "respond",
                    "message": f"Patched {path}: {summary}",
                    "reason": "Stop because the requested patch was blocked.",
                }
            if not prior.get("ok", False):
                if not read_trace:
                    return {
                        "action": "read_file",
                        "path": path,
                        "max_chars": 4000,
                        "reason": "Inspect the file after the patch failure so we can recover safely.",
                    }
                if not patch.get("start_anchor"):
                    current_text = read_content(read_trace.get("output_summary", ""))
                    old = str(patch.get("old", ""))
                    if current_text.count(old) > 1 and not patch.get("replace_all"):
                        retry_patch = dict(patch)
                        retry_patch["replace_all"] = True
                        retry_patch["occurrence"] = None
                        return patch_action_from_request(
                            retry_patch,
                            reason="Retry the requested patch by replacing all inspected matches.",
                        )
                if patch.get("start_anchor"):
                    rewritten = rewrite_anchor_block_from_summary(
                        read_trace.get("output_summary", ""),
                        str(patch.get("start_anchor", "")),
                        str(patch.get("end_anchor", "")),
                        str(patch["new"]),
                        include_anchors=bool(patch.get("include_anchors", False)),
                    )
                else:
                    rewritten = rewrite_text_from_summary(
                        read_trace.get("output_summary", ""),
                        str(patch.get("old", "")),
                        str(patch["new"]),
                    )
                if rewritten is None:
                    return {
                        "action": "respond",
                        "message": f"Patched {path}: {summary}",
                        "reason": "Stop because the file contents do not support a safe rewrite recovery.",
                    }
                if not write_trace:
                    return {
                        "action": "write_file",
                        "path": path,
                        "text": rewritten,
                        "reason": "Recover from the failed patch by rewriting the file from inspected contents.",
                    }
                write_summary = write_trace.get("output_summary") or "write completed"
                return {
                    "action": "respond",
                    "message": f"Recovered {path} with a rewrite: {write_summary}",
                    "reason": "The requested patch was recovered via write_file.",
                }
            return {
                "action": "respond",
                "message": f"Patched {path}: {summary}",
                "reason": "The requested patch is already recorded in task state.",
            }
        return patch_action_from_request(patch, reason="Need to apply the requested patch before responding.")

    def _mock_write_file_action(self, request: str, linked_tools: list[dict[str, Any]]) -> dict[str, Any] | None:
        write = extract_write_instruction(request)
        if not write:
            return None
        path, text = write
        prior = find_tool_trace(linked_tools, "write_file", path)
        if prior:
            summary = prior.get("output_summary") or "file was written"
            return {
                "action": "respond",
                "message": f"Wrote {path}: {summary}",
                "reason": "The requested file write is already recorded in task state.",
            }
        return {
            "action": "write_file",
            "path": path,
            "text": text,
            "reason": "Need to create or overwrite the requested file before responding.",
        }


class ChattyMockProvider(MockProvider):
    name = "mock-chatty"

    def run(self, packet: dict[str, Any]) -> ProviderResponse:
        response = super().run(packet)
        mode = str(packet.get("agent", {}).get("mode", ""))
        if not mode.startswith("tool_planning_v"):
            return response
        text = f"Here is the next action:\n```json\n{response.text}\n```"
        return ProviderResponse(
            text=text,
            input_tokens=response.input_tokens,
            output_tokens=estimate_tokens(text),
        )


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 120,
    ):
        self.model = model or env_value("CONTEXT_KERNEL_OPENAI_MODEL") or "gpt-5.5"
        self.base_url = normalize_openai_base_url(base_url or env_value("CONTEXT_KERNEL_OPENAI_BASE_URL") or "")
        self.api_key = api_key or env_value("CONTEXT_KERNEL_OPENAI_API_KEY")
        self.timeout_seconds = timeout_seconds
        if not self.base_url:
            raise ValueError("Missing CONTEXT_KERNEL_OPENAI_BASE_URL for OpenAI-compatible provider.")
        if not self.api_key:
            raise ValueError("Missing CONTEXT_KERNEL_OPENAI_API_KEY for OpenAI-compatible provider.")

    def run(self, packet: dict[str, Any]) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": build_messages(packet),
        }
        response = self._post_json("/chat/completions", payload)
        text = extract_text(response)
        usage = response.get("usage", {})
        input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or estimate_tokens(packet)
        output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or estimate_tokens(text)
        return ProviderResponse(
            text=text,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
        )

    def list_models(self) -> list[str]:
        payload = self._get_json("/models")
        models = payload.get("data", payload if isinstance(payload, list) else [])
        names: list[str] = []
        for item in models:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    names.append(str(model_id))
            else:
                names.append(str(item))
        return sorted(names)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._open_json(request)

    def _get_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            headers={"Authorization": "Bearer " + self.api_key},
            method="GET",
        )
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    preview = body[:500].replace("\n", " ")
                    raise RuntimeError(f"Provider returned invalid JSON: {preview}") from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Provider HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Provider network error: {exc}") from exc


def build_messages(packet: dict[str, Any]) -> list[dict[str, str]]:
    context_json = json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True)
    system = (
        "You are running inside Context Kernel. Use the provided context packet only. "
        "Prefer concise, task-focused answers and mention missing context when relevant."
    )
    if packet.get("agent", {}).get("response_contract"):
        system += " Follow the agent response contract exactly and return only the requested JSON object."
    return [
        {
            "role": "system",
            "content": system,
        },
        {
            "role": "user",
            "content": "Context packet:\n```json\n" + context_json + "\n```",
        },
    ]


def extract_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(message, dict):
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            return "".join(parts)
    return str(choice.get("text", "")) if isinstance(choice, dict) else ""


def get_provider(name: str, model: str | None = None, base_url: str | None = None) -> ModelProvider:
    if name == "mock":
        return MockProvider()
    if name == "mock-chatty":
        return ChattyMockProvider()
    if name == "openai":
        return OpenAICompatibleProvider(model=model, base_url=base_url)
    raise ValueError(f"Unsupported provider for MVP: {name}")


def list_provider_models(name: str, base_url: str | None = None) -> list[str]:
    if name == "openai":
        return OpenAICompatibleProvider(base_url=base_url).list_models()
    if name == "mock":
        return ["mock"]
    if name == "mock-chatty":
        return ["mock-chatty"]
    raise ValueError(f"Unsupported provider for model listing: {name}")


def env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    env_file = project_env_values()
    return env_file.get(name)


def project_env_values() -> dict[str, str]:
    for directory in [Path.cwd(), *Path.cwd().parents]:
        path = directory / ".env"
        if path.exists():
            return parse_env_file(path)
    project_root = os.environ.get("CONTEXT_KERNEL_PROJECT_ROOT")
    if project_root:
        path = Path(project_root) / ".env"
        if path.exists():
            return parse_env_file(path)
    return {}


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip().lstrip("\ufeff")] = value
    return values


def normalize_openai_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith("/v1"):
        return normalized
    return normalized + "/v1"


def find_tool_trace(
    traces: list[dict[str, Any]],
    tool_name: str,
    subject_fragment: str,
) -> dict[str, Any] | None:
    normalized_fragment = normalize_subject(subject_fragment)
    for trace in reversed(traces):
        if trace.get("tool") != tool_name:
            continue
        if normalized_fragment in normalize_subject(str(trace.get("subject", ""))):
            return trace
    return None


def find_command_trace(traces: list[dict[str, Any]], command: str) -> dict[str, Any] | None:
    matches = find_command_traces(traces, command)
    return matches[-1] if matches else None


def find_command_traces(traces: list[dict[str, Any]], command: str) -> list[dict[str, Any]]:
    normalized_command = " ".join(command.split()).casefold()
    matches: list[dict[str, Any]] = []
    for trace in reversed(traces):
        if trace.get("tool") != "run_command":
            continue
        subject = " ".join(str(trace.get("subject", "")).split()).casefold()
        summary = " ".join(str(trace.get("output_summary", "")).split()).casefold()
        if normalized_command and (normalized_command in subject or normalized_command in summary):
            matches.append(trace)
    return list(reversed(matches))


def normalize_subject(text: str) -> str:
    return text.replace("\\", "/").casefold()


def extract_anchor_patch_instruction(request: str) -> tuple[str, str, str, str, bool] | None:
    patterns = [
        r"(?is)\bpatch\s+([^\s]+)\s+between\s+([\"'])(.*?)\2\s+and\s+([\"'])(.*?)\4\s+with\s+([\"'])(.*?)\6",
        r"(?is)\bpatch\s+([^\s]+)\s+replace\s+block\s+between\s+([\"'])(.*?)\2\s+and\s+([\"'])(.*?)\4\s+with\s+([\"'])(.*?)\6",
    ]
    for pattern in patterns:
        match = re.search(pattern, request)
        if not match:
            continue
        path, _, start_anchor, _, end_anchor, _, new = match.groups()
        include_anchors = request_prefers_include_anchors(request)
        return path.strip(), start_anchor, end_anchor, new, include_anchors
    return None


def extract_patch_instruction(request: str) -> tuple[str, str, str] | None:
    match = re.search(r"(?is)\bpatch\s+([^\s]+)\s+replace(?:\s+all)?\s+([\"'])(.*?)\2\s+with\s+([\"'])(.*?)\4", request)
    if not match:
        return None
    path, _, old, _, new = match.groups()
    return path.strip(), old, new


def extract_patch_request(request: str) -> dict[str, Any] | None:
    anchor_patch = extract_anchor_patch_instruction(request)
    if anchor_patch:
        path, start_anchor, end_anchor, new, include_anchors = anchor_patch
        return {
            "path": path,
            "new": new,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "include_anchors": include_anchors,
        }

    patch = extract_patch_instruction(request)
    if not patch:
        return None
    path, old, new = patch
    return {
        "path": path,
        "old": old,
        "new": new,
        "replace_all": request_prefers_replace_all(request),
        "occurrence": None,
    }


def extract_batch_patch_requests(request: str) -> list[dict[str, Any]]:
    clauses = [clause.strip() for clause in re.split(r";|\n+", request) if clause.strip()]
    patches: list[dict[str, Any]] = []
    for clause in clauses:
        patch = extract_patch_request(clause)
        if patch:
            patches.append(patch_edit_from_request(patch))
    return patches if len(patches) >= 2 else []


def patch_edit_from_request(patch: dict[str, Any]) -> dict[str, Any]:
    action = patch_action_from_request(patch, reason="")
    action.pop("action", None)
    action.pop("reason", None)
    return action


def extract_write_instruction(request: str) -> tuple[str, str] | None:
    match = re.search(r"(?is)\b(?:write|create)\s+([^\s]+)\s+with\s+([\"'])(.*?)\2", request)
    if not match:
        return None
    path, _, text = match.groups()
    return path.strip(), text


def extract_requested_command(request: str) -> str | None:
    patterns = [
        r"(?is)\brun\s+command\s+(.+)$",
        r"(?is)\brun\s+`([^`]+)`",
        r"(?is)\bverify\s+with\s+command\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, request)
        if not match:
            continue
        command = trim_command_tail(match.group(1).strip())
        if command:
            return command
    return None


def resolve_requested_command(request: str, project_commands: dict[str, str] | None = None) -> str | None:
    explicit = extract_requested_command(request)
    if explicit:
        return explicit
    return project_profile_command(request, project_commands or {})


def project_profile_command(request: str, project_commands: dict[str, str]) -> str | None:
    if not isinstance(project_commands, dict) or not project_commands:
        return None
    lower = request.casefold()
    preferences = [
        ("test", ["run tests", "run the tests", "test suite", "tests", "test", "verify", "verification"]),
        ("lint", ["lint", "style check", "static check"]),
        ("build", ["build", "compile"]),
        ("install", ["install dependencies", "install", "setup"]),
    ]
    for name, markers in preferences:
        command = project_commands.get(name)
        if isinstance(command, str) and command.strip() and any(marker in lower for marker in markers):
            return command.strip()
    return None


def asks_to_fix_tests(request: str) -> bool:
    lower = request.casefold()
    return any(marker in lower for marker in ["fix failing test", "fix the failing test", "fix tests", "fix the tests"])


def failure_path_from_summary(summary: str) -> str | None:
    paths = failure_paths_from_summary(summary, limit=1)
    return paths[0] if paths else None


def failure_paths_from_summary(summary: str, *, limit: int = 5) -> list[str]:
    text = str(summary)
    candidates = re.findall(r"File\s+\"([^\"]+\.py)\",\s+line\s+\d+", text)
    candidates.extend(re.findall(r"((?:[A-Za-z]:)?[^\s:]+\.py):\d+", text))
    paths: list[str] = []
    seen: set[str] = set()
    for candidate in reversed(candidates):
        normalized = candidate.strip().strip('"').strip("'").replace("\\", "/")
        if "=" in normalized:
            normalized = normalized.rsplit("=", 1)[-1]
        if any(part in normalized for part in ["/.venv/", "/site-packages/", "/.akernel/"]):
            continue
        normalized = normalized.lstrip("./")
        if normalized in seen:
            continue
        seen.add(normalized)
        paths.append(normalized)
        if len(paths) >= limit:
            break
    return paths


def infer_batch_patch_from_failures(read_traces: list[dict[str, Any]], failure_summary: str) -> dict[str, Any] | None:
    edits = []
    seen_paths: set[str] = set()
    for trace in read_traces:
        patch = infer_patch_from_failure(trace, failure_summary)
        if not patch or patch.get("action") != "patch_file":
            continue
        path = str(patch.get("path", ""))
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        edits.append(
            {
                key: patch[key]
                for key in ["path", "old", "new", "replace_all", "occurrence", "start_anchor", "end_anchor", "include_anchors"]
                if key in patch and patch[key] not in {None, ""}
            }
        )
    if len(edits) < 2:
        return None
    return {
        "action": "batch_patch",
        "edits": edits,
        "reason": "Apply the inferred multi-file fix as one rollback-safe batch patch.",
    }


def infer_patch_from_failure(read_trace: dict[str, Any], failure_summary: str) -> dict[str, Any] | None:
    path = str(read_trace.get("subject") or read_trace.get("path") or "")
    if not path:
        path = str(read_trace.get("output", {}).get("path", ""))
    summary = read_trace.get("output_summary", "")
    content = read_content(summary)
    expected = expected_number_from_failure(failure_summary)
    actual = actual_number_from_failure(failure_summary)

    if expected is not None and actual is not None and f"return {actual}" in content:
        return {
            "action": "patch_file",
            "path": path,
            "old": f"return {actual}",
            "new": f"return {expected}",
            "reason": "Patch the implementation value suggested by the failing assertion.",
        }
    if expected is not None and "raise ValueError" in content:
        line = first_matching_statement(content, "raise ValueError")
        if line:
            return {
                "action": "patch_file",
                "path": path,
                "old": line,
                "new": f"return {expected}",
                "reason": "Replace the failing exception with the value expected by the test.",
            }
    if "return False" in content and re.search(r"(?i)\btrue\b", failure_summary):
        return {
            "action": "patch_file",
            "path": path,
            "old": "return False",
            "new": "return True",
            "reason": "Patch the boolean return value suggested by the failing assertion.",
        }
    return None


def expected_number_from_failure(text: str) -> str | None:
    match = re.search(r"assert\s+(-?\d+)\s*==\s*(-?\d+)", str(text))
    if match:
        return match.group(2)
    match = re.search(r"expected\s+(-?\d+)", str(text), flags=re.IGNORECASE)
    return match.group(1) if match else None


def actual_number_from_failure(text: str) -> str | None:
    match = re.search(r"assert\s+(-?\d+)\s*==\s*(-?\d+)", str(text))
    if match:
        return match.group(1)
    return None


def first_matching_statement(content: str, prefix: str) -> str | None:
    for part in re.split(r"\s{2,}|\n", content):
        stripped = part.strip()
        if stripped.startswith(prefix):
            return stripped
    return None


def read_content(summary: str) -> str:
    text = str(summary)
    suffix = " (truncated)"
    if text.endswith(suffix):
        text = text[: -len(suffix)]
    return text


def rewrite_text_from_summary(summary: str, old: str, new: str) -> str | None:
    content = read_content(summary)
    if old not in content:
        return None
    return content.replace(old, new)


def rewrite_anchor_block_from_summary(
    summary: str,
    start_anchor: str,
    end_anchor: str,
    new: str,
    *,
    include_anchors: bool,
) -> str | None:
    content = read_content(summary)
    start_index = content.find(start_anchor)
    if start_index < 0:
        return None
    end_index = content.find(end_anchor, start_index + len(start_anchor))
    if end_index < 0:
        return None

    replace_start = start_index if include_anchors else start_index + len(start_anchor)
    replace_end = end_index + len(end_anchor) if include_anchors else end_index
    original = content[replace_start:replace_end]
    replacement = normalize_anchor_replacement(new, original)
    return content[:replace_start] + replacement + content[replace_end:]


def rewrite_to_include_text(current_text: str, desired_text: str) -> str:
    if current_text:
        if current_text.endswith("\n"):
            return current_text + desired_text
        return current_text + "\n" + desired_text
    return desired_text


def trim_command_tail(command: str) -> str:
    text = command.strip().strip("`")
    lower = text.casefold()
    for marker in [
        " and tell me",
        " and summarize",
        " and report",
        " then tell me",
        " then summarize",
        " then report",
    ]:
        index = lower.find(marker)
        if index > 0:
            text = text[:index].rstrip()
            break
    return text.rstrip(" .!?;:")


def request_prefers_replace_all(request: str) -> bool:
    return bool(re.search(r"(?is)\breplace\s+all\s+[\"']", request))


def request_prefers_include_anchors(request: str) -> bool:
    return "including anchors" in request.casefold()


def run_command_or_policy_response(command: str, allowed_roots: set[str], *, reason: str) -> dict[str, Any]:
    if command_allowed(command, allowed_roots):
        return {
            "action": "run_command",
            "command": command,
            "timeout_seconds": 30,
            "reason": reason,
        }
    rendered_roots = ", ".join(sorted(allowed_roots)) if allowed_roots else "none"
    return {
        "action": "respond",
        "message": f"Command `{command}` is outside the workspace allowlist. Allowed roots: {rendered_roots}.",
        "reason": "Stop because the requested command root is not allowed by runtime.command_policy.",
    }


def command_allowed(command: str, allowed_roots: set[str]) -> bool:
    if not allowed_roots:
        return True
    return bool(command_root_candidates(command).intersection(allowed_roots))


def patch_action_from_request(patch: dict[str, Any], *, reason: str) -> dict[str, Any]:
    action = {
        "action": "patch_file",
        "path": str(patch["path"]),
        "new": str(patch["new"]),
        "reason": reason,
    }
    if patch.get("start_anchor"):
        action["start_anchor"] = str(patch["start_anchor"])
        action["end_anchor"] = str(patch["end_anchor"])
        action["include_anchors"] = bool(patch.get("include_anchors", False))
        return action

    action["old"] = str(patch.get("old", ""))
    action["replace_all"] = bool(patch.get("replace_all", False))
    if patch.get("occurrence") is not None:
        action["occurrence"] = int(patch["occurrence"])
    return action


def normalize_anchor_replacement(new: str, original: str) -> str:
    replacement = new
    if original.startswith("\r\n") and not replacement.startswith(("\r", "\n")):
        replacement = "\r\n" + replacement
    elif original.startswith("\n") and not replacement.startswith(("\r", "\n")):
        replacement = "\n" + replacement

    if original.endswith("\r\n") and not replacement.endswith(("\r", "\n")):
        replacement = replacement + "\r\n"
    elif original.endswith("\n") and not replacement.endswith(("\r", "\n")):
        replacement = replacement + "\n"
    return replacement
