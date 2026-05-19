from __future__ import annotations

import shlex
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import utc_now
from .policy import check_batch_file_policy, check_command_policy, check_file_policy, command_root, resolve_target
from .storage import Workspace
from .tool_transactions import begin_file_transaction


MAX_CAPTURE_CHARS = 8000
MAX_LIST_ENTRIES = 200
TRANSACTION_FILE_ACTIONS = {"create_file", "write_file", "append_file", "patch_file"}
TRANSACTION_COMMAND_ACTIONS = {"run_command"}
TRANSACTION_ACTIONS = TRANSACTION_FILE_ACTIONS | TRANSACTION_COMMAND_ACTIONS


class ToolExecutor:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def list_dir(self, path: str = ".", *, limit: int = MAX_LIST_ENTRIES) -> dict[str, Any]:
        policy = check_file_policy(self.workspace, "read", path)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("list_dir", policy))

        target = Path(policy["subject"])
        if not target.exists():
            result = tool_result("list_dir", policy, ok=False, error=f"Directory does not exist: {target}")
            return self._write_trace(result)
        if not target.is_dir():
            result = tool_result("list_dir", policy, ok=False, error=f"Target is not a directory: {target}")
            return self._write_trace(result)

        entries = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        limited = entries[: max(1, min(limit, MAX_LIST_ENTRIES))]
        result = tool_result(
            "list_dir",
            policy,
            ok=True,
            output={
                "path": str(target),
                "entries": [
                    {
                        "name": item.name,
                        "path": str(item.relative_to(self.workspace.root)) if is_relative_path(item, self.workspace.root) else str(item),
                        "kind": "dir" if item.is_dir() else "file",
                        "size_bytes": item.stat().st_size if item.is_file() else None,
                    }
                    for item in limited
                ],
                "total_entries": len(entries),
                "truncated": len(entries) > len(limited),
            },
        )
        return self._write_trace(result)

    def file_info(self, path: str) -> dict[str, Any]:
        policy = check_file_policy(self.workspace, "read", path)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("file_info", policy))

        target = Path(policy["subject"])
        if not target.exists():
            result = tool_result(
                "file_info",
                policy,
                ok=True,
                output={"path": str(target), "exists": False},
            )
            return self._write_trace(result)
        stat = target.stat()
        result = tool_result(
            "file_info",
            policy,
            ok=True,
            output={
                "path": str(target),
                "exists": True,
                "kind": "dir" if target.is_dir() else "file" if target.is_file() else "other",
                "size_bytes": stat.st_size if target.is_file() else None,
                "modified_at": utc_now_from_timestamp(stat.st_mtime),
            },
        )
        return self._write_trace(result)

    def read_file(self, path: str, *, max_chars: int = MAX_CAPTURE_CHARS) -> dict[str, Any]:
        policy = check_file_policy(self.workspace, "read", path)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("read_file", policy))

        target = Path(policy["subject"])
        if not target.exists():
            result = tool_result("read_file", policy, ok=False, error=f"File does not exist: {target}")
            return self._write_trace(result)
        if not target.is_file():
            result = tool_result("read_file", policy, ok=False, error=f"Target is not a file: {target}")
            return self._write_trace(result)

        text = target.read_text(encoding="utf-8-sig", errors="replace")
        truncated = len(text) > max_chars
        result = tool_result(
            "read_file",
            policy,
            ok=True,
            output={
                "path": str(target),
                "content": text[:max_chars],
                "truncated": truncated,
                "size_chars": len(text),
            },
        )
        return self._write_trace(result)

    def create_file(self, path: str, text: str) -> dict[str, Any]:
        policy = check_file_policy(self.workspace, "write", path)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("create_file", policy))

        target = resolve_target(self.workspace, Path(path))
        if target.exists():
            result = tool_result(
                "create_file",
                policy,
                ok=False,
                output={"path": str(target), "exists": True},
                error=f"File already exists: {target}",
            )
            return self._write_trace(result)
        target.parent.mkdir(parents=True, exist_ok=True)
        transaction = begin_file_transaction(self.workspace, [path], label="create_file")
        try:
            atomic_create_text(target, text)
        except FileExistsError:
            result = tool_result(
                "create_file",
                policy,
                ok=False,
                output={"path": str(target), "exists": True},
                error=f"File already exists: {target}",
            )
            return self._write_trace(result)
        except OSError as exc:
            result = tool_result(
                "create_file",
                policy,
                ok=False,
                output={"transaction": transaction.rollback_output()},
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._write_trace(result)
        result = tool_result(
            "create_file",
            policy,
            ok=True,
            output={
                "path": str(target),
                "written_chars": len(text),
                "created": True,
                "overwritten": False,
                "transaction": transaction.commit_output(),
            },
        )
        return self._write_trace(result)

    def append_file(self, path: str, text: str, *, create: bool = True) -> dict[str, Any]:
        policy = check_file_policy(self.workspace, "write", path)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("append_file", policy))

        target = resolve_target(self.workspace, Path(path))
        if target.exists() and not target.is_file():
            result = tool_result("append_file", policy, ok=False, error=f"Target is not a file: {target}")
            return self._write_trace(result)
        if not target.exists() and not create:
            result = tool_result("append_file", policy, ok=False, error=f"File does not exist: {target}")
            return self._write_trace(result)

        existed = target.exists()
        before_chars = target.stat().st_size if existed else 0
        target.parent.mkdir(parents=True, exist_ok=True)
        transaction = begin_file_transaction(self.workspace, [path], label="append_file")
        try:
            with target.open("a", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as exc:
            result = tool_result(
                "append_file",
                policy,
                ok=False,
                output={"transaction": transaction.rollback_output()},
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._write_trace(result)
        result = tool_result(
            "append_file",
            policy,
            ok=True,
            output={
                "path": str(target),
                "created": not existed,
                "appended_chars": len(text),
                "size_before_bytes": before_chars,
                "size_after_bytes": target.stat().st_size,
                "transaction": transaction.commit_output(),
            },
        )
        return self._write_trace(result)

    def write_file(self, path: str, text: str, *, overwrite: bool = True) -> dict[str, Any]:
        policy = check_file_policy(self.workspace, "write", path)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("write_file", policy))

        target = resolve_target(self.workspace, Path(path))
        existed = target.exists()
        if existed and not target.is_file():
            result = tool_result("write_file", policy, ok=False, error=f"Target is not a file: {target}")
            return self._write_trace(result)
        if existed and not overwrite:
            result = tool_result(
                "create_file",
                policy,
                ok=False,
                output={"path": str(target), "exists": True},
                error=f"File already exists: {target}",
            )
            return self._write_trace(result)
        target.parent.mkdir(parents=True, exist_ok=True)
        transaction = begin_file_transaction(self.workspace, [path], label="write_file" if overwrite else "create_file")
        try:
            atomic_write_text(target, text)
        except OSError as exc:
            result = tool_result(
                "write_file" if overwrite else "create_file",
                policy,
                ok=False,
                output={"transaction": transaction.rollback_output()},
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._write_trace(result)
        result = tool_result(
            "write_file" if overwrite else "create_file",
            policy,
            ok=True,
            output={
                "path": str(target),
                "written_chars": len(text),
                "created": not existed,
                "overwritten": existed and overwrite,
                "transaction": transaction.commit_output(),
            },
        )
        return self._write_trace(result)

    def patch_file(
        self,
        path: str,
        old: str = "",
        new: str = "",
        *,
        replace_all: bool = False,
        occurrence: int | None = None,
        start_anchor: str | None = None,
        end_anchor: str | None = None,
        include_anchors: bool = False,
    ) -> dict[str, Any]:
        policy = check_file_policy(self.workspace, "write", path)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("patch_file", policy))
        anchor_mode = start_anchor is not None or end_anchor is not None
        if anchor_mode:
            if not start_anchor or not end_anchor:
                result = tool_result(
                    "patch_file",
                    policy,
                    ok=False,
                    error="Anchor patch requires both start_anchor and end_anchor.",
                )
                return self._write_trace(result)
            if replace_all or occurrence is not None:
                result = tool_result(
                    "patch_file",
                    policy,
                    ok=False,
                    error="Anchor patch cannot combine start/end anchors with replace_all or occurrence.",
                )
                return self._write_trace(result)
        elif not old:
            result = tool_result("patch_file", policy, ok=False, error="Patch old text cannot be empty.")
            return self._write_trace(result)
        if replace_all and occurrence is not None:
            result = tool_result(
                "patch_file",
                policy,
                ok=False,
                error="Patch cannot use replace_all and occurrence at the same time.",
            )
            return self._write_trace(result)
        if occurrence is not None and occurrence < 1:
            result = tool_result("patch_file", policy, ok=False, error="Patch occurrence must be at least 1.")
            return self._write_trace(result)

        target = resolve_target(self.workspace, Path(path))
        if not target.exists():
            result = tool_result("patch_file", policy, ok=False, error=f"File does not exist: {target}")
            return self._write_trace(result)
        if not target.is_file():
            result = tool_result("patch_file", policy, ok=False, error=f"Target is not a file: {target}")
            return self._write_trace(result)

        text = target.read_text(encoding="utf-8", errors="replace")
        if anchor_mode:
            blocks = find_anchor_blocks(text, start_anchor, end_anchor, include_anchors=include_anchors)
            if not blocks:
                result = tool_result(
                    "patch_file",
                    policy,
                    ok=False,
                    output={
                        "start_matches": text.count(start_anchor),
                        "end_matches": text.count(end_anchor),
                        "block_matches": 0,
                    },
                    error="Anchor patch could not find an ordered start/end block.",
                )
                return self._write_trace(result)
            if len(blocks) != 1:
                result = tool_result(
                    "patch_file",
                    policy,
                    ok=False,
                    output={
                        "start_matches": text.count(start_anchor),
                        "end_matches": text.count(end_anchor),
                        "block_matches": len(blocks),
                    },
                    error=f"Anchor patch must match exactly one block; found {len(blocks)}.",
                )
                return self._write_trace(result)

            block = blocks[0]
            replacement = normalize_block_replacement(new, block["original"])
            updated = text[: block["replace_start"]] + replacement + text[block["replace_end"] :]
            transaction = begin_file_transaction(self.workspace, [path], label="patch_file")
            try:
                atomic_write_text(target, updated)
            except OSError as exc:
                result = tool_result(
                    "patch_file",
                    policy,
                    ok=False,
                    output={"transaction": transaction.rollback_output()},
                    error=f"{type(exc).__name__}: {exc}",
                )
                return self._write_trace(result)
            result = tool_result(
                "patch_file",
                policy,
                ok=True,
                output={
                    "path": str(target),
                    "old_chars": len(block["original"]),
                    "new_chars": len(replacement),
                    "delta_chars": len(replacement) - len(block["original"]),
                    "matches": len(blocks),
                    "replacement_count": 1,
                    "mode": "anchor_inclusive" if include_anchors else "anchor_between",
                    "start_anchor": start_anchor,
                    "end_anchor": end_anchor,
                    "transaction": transaction.commit_output(),
                },
            )
            return self._write_trace(result)

        matches = text.count(old)
        if matches < 1:
            result = tool_result(
                "patch_file",
                policy,
                ok=False,
                output={"matches": matches},
                error="Patch old text was not found.",
            )
            return self._write_trace(result)
        if occurrence is not None and occurrence > matches:
            result = tool_result(
                "patch_file",
                policy,
                ok=False,
                output={"matches": matches},
                error=f"Patch occurrence {occurrence} is out of range; found {matches} matches.",
            )
            return self._write_trace(result)
        if not replace_all and occurrence is None and matches != 1:
            result = tool_result(
                "patch_file",
                policy,
                ok=False,
                output={"matches": matches},
                error=f"Patch old text must match exactly once; found {matches}.",
            )
            return self._write_trace(result)

        replacement_mode = "single"
        replacement_count = 1
        if replace_all:
            updated = text.replace(old, new)
            replacement_mode = "replace_all"
            replacement_count = matches
        elif occurrence is not None:
            updated = replace_nth_occurrence(text, old, new, occurrence)
            replacement_mode = f"occurrence:{occurrence}"
        else:
            updated = text.replace(old, new, 1)
        transaction = begin_file_transaction(self.workspace, [path], label="patch_file")
        try:
            atomic_write_text(target, updated)
        except OSError as exc:
            result = tool_result(
                "patch_file",
                policy,
                ok=False,
                output={"transaction": transaction.rollback_output()},
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._write_trace(result)
        result = tool_result(
            "patch_file",
            policy,
            ok=True,
            output={
                "path": str(target),
                "old_chars": len(old),
                "new_chars": len(new),
                "delta_chars": len(new) - len(old),
                "matches": matches,
                "replacement_count": replacement_count,
                "mode": replacement_mode,
                "transaction": transaction.commit_output(),
            },
        )
        return self._write_trace(result)

    def batch_patch(self, edits: list[dict[str, Any]]) -> dict[str, Any]:
        normalized_edits = [normalize_patch_edit(edit) for edit in edits]
        policy = check_batch_file_policy(self.workspace, normalized_edits)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("batch_patch", policy))

        transaction = begin_file_transaction(
            self.workspace,
            [edit["path"] for edit in normalized_edits],
            label="batch_patch",
        )
        results: list[dict[str, Any]] = []
        for index, edit in enumerate(normalized_edits, start=1):
            result = self.patch_file(
                edit["path"],
                edit.get("old", ""),
                edit.get("new", ""),
                replace_all=bool(edit.get("replace_all", False)),
                occurrence=edit.get("occurrence"),
                start_anchor=edit.get("start_anchor"),
                end_anchor=edit.get("end_anchor"),
                include_anchors=bool(edit.get("include_anchors", False)),
            )
            results.append(batch_child_summary(index, result))
            if result["blocked"] or not result["ok"]:
                transaction_output = transaction.rollback_output()
                return self._write_trace(
                    tool_result(
                        "batch_patch",
                        policy,
                        ok=False,
                        output={
                            "applied_count": max(0, index - 1),
                            "rolled_back": True,
                            "transaction": transaction_output,
                            "results": results,
                        },
                        error=f"Batch patch failed at edit {index}: {result.get('error') or result['policy']['status']}",
                    )
                )

        return self._write_trace(
            tool_result(
                "batch_patch",
                policy,
                ok=True,
                output={
                    "applied_count": len(results),
                    "rolled_back": False,
                    "transaction": transaction.commit_output(),
                    "results": results,
                    "subtrace_ids": [item["trace_id"] for item in results],
                },
            )
        )

    def transaction(
        self,
        steps: list[dict[str, Any]],
        *,
        allow_destructive_commands: bool = False,
    ) -> dict[str, Any]:
        normalized_steps = normalize_transaction_steps(steps)
        file_paths = transaction_file_paths(normalized_steps)
        command_policies = transaction_command_policies(
            self.workspace,
            normalized_steps,
            allow_destructive_commands=allow_destructive_commands,
        )
        safety = transaction_safety_summary(self.workspace, normalized_steps, command_policies=command_policies)
        policy = transaction_policy(self.workspace, file_paths)
        if not policy["allowed"]:
            failure = transaction_preflight_failure("file_policy", policy)
            return self._write_trace(blocked_result("transaction", policy, output={"safety": safety, "failure": failure}))

        command_policy = transaction_command_policy(command_policies)
        if command_policy is not None and not command_policy["allowed"]:
            failure = transaction_preflight_failure("command_policy", command_policy)
            return self._write_trace(blocked_result("transaction", command_policy, output={"safety": safety, "failure": failure}))

        transaction = begin_file_transaction(self.workspace, file_paths, label="transaction")
        results: list[dict[str, Any]] = []
        for index, step in enumerate(normalized_steps, start=1):
            result = self.execute_transaction_step(
                step,
                allow_destructive_commands=allow_destructive_commands,
            )
            results.append(batch_child_summary(index, result))
            if result["blocked"] or not result["ok"]:
                transaction_output = transaction.rollback_output()
                failure_reason = child_failure_reason(result)
                failure = transaction_step_failure(index, step, result, failure_reason)
                return self._write_trace(
                    tool_result(
                        "transaction",
                        policy,
                        ok=False,
                        output={
                            "applied_count": max(0, index - 1),
                            "rolled_back": True,
                            "safety": safety,
                            "failure": failure,
                            "transaction": transaction_output,
                            "results": results,
                            "subtrace_ids": [item["trace_id"] for item in results],
                        },
                        error=f"Transaction failed at step {index}: {failure_reason}",
                    )
                )

        return self._write_trace(
            tool_result(
                "transaction",
                policy,
                ok=True,
                output={
                    "applied_count": len(results),
                    "rolled_back": False,
                    "safety": safety,
                    "transaction": transaction.commit_output(),
                    "results": results,
                    "subtrace_ids": [item["trace_id"] for item in results],
                },
            )
        )

    def execute_transaction_step(
        self,
        step: dict[str, Any],
        *,
        allow_destructive_commands: bool,
    ) -> dict[str, Any]:
        action = step["action"]
        if action == "create_file":
            return self.create_file(step["path"], step["text"])
        if action == "write_file":
            return self.write_file(step["path"], step["text"])
        if action == "append_file":
            return self.append_file(step["path"], step["text"], create=bool(step.get("create", True)))
        if action == "patch_file":
            return self.patch_file(
                step["path"],
                step.get("old", ""),
                step.get("new", ""),
                replace_all=bool(step.get("replace_all", False)),
                occurrence=step.get("occurrence"),
                start_anchor=step.get("start_anchor"),
                end_anchor=step.get("end_anchor"),
                include_anchors=bool(step.get("include_anchors", False)),
            )
        return self.run_command(
            step["command"],
            allow_destructive=allow_destructive_commands,
            timeout_seconds=step["timeout_seconds"],
        )

    def delete_file(self, path: str, *, allow_destructive: bool = False) -> dict[str, Any]:
        policy = check_file_policy(self.workspace, "delete", path, allow_destructive=allow_destructive)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("delete_file", policy))

        target = resolve_target(self.workspace, Path(path))
        if not target.exists():
            result = tool_result("delete_file", policy, ok=False, error=f"File does not exist: {target}")
            return self._write_trace(result)
        if not target.is_file():
            result = tool_result("delete_file", policy, ok=False, error=f"Target is not a file: {target}")
            return self._write_trace(result)

        size = target.stat().st_size
        target.unlink()
        result = tool_result(
            "delete_file",
            policy,
            ok=True,
            output={"path": str(target), "deleted_bytes": size},
        )
        return self._write_trace(result)

    def run_command(
        self,
        command: str,
        *,
        allow_destructive: bool = False,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        policy = check_command_policy(command, workspace=self.workspace, allow_destructive=allow_destructive)
        if not policy["allowed"]:
            return self._write_trace(blocked_result("run_command", policy))

        args = split_command(command)
        try:
            completed = subprocess.run(
                args,
                cwd=self.workspace.root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
            )
        except FileNotFoundError as exc:
            result = tool_result("run_command", policy, ok=False, error=str(exc))
            return self._write_trace(result)
        except subprocess.TimeoutExpired as exc:
            result = tool_result(
                "run_command",
                policy,
                ok=False,
                output={
                    "command": command,
                    "stdout": trim_capture(exc.stdout or ""),
                    "stderr": trim_capture(exc.stderr or ""),
                    "timeout_seconds": timeout_seconds,
                },
                error="command timed out",
            )
            return self._write_trace(result)
        result = tool_result(
            "run_command",
            policy,
            ok=completed.returncode == 0,
            output={
                "command": command,
                "exit_code": completed.returncode,
                "stdout": trim_capture(completed.stdout),
                "stderr": trim_capture(completed.stderr),
                "stdout_truncated": len(completed.stdout) > MAX_CAPTURE_CHARS,
                "stderr_truncated": len(completed.stderr) > MAX_CAPTURE_CHARS,
            },
        )
        return self._write_trace(result)

    def record_external_tool(
        self,
        tool: str,
        *,
        subject: str,
        output: dict[str, Any] | None = None,
        ok: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        policy = {
            "allowed": True,
            "status": "allowed",
            "operation": tool,
            "subject": subject,
            "reason": "manual external tool invocation",
        }
        return self._write_trace(tool_result(tool, policy, ok=ok, output=output, error=error))

    def list_traces(self) -> list[dict[str, Any]]:
        self.workspace.tool_traces_dir.mkdir(parents=True, exist_ok=True)
        items: list[dict[str, Any]] = []
        for path in sorted(self.workspace.tool_traces_dir.glob("*.json")):
            trace = Workspace.read_json(path)
            items.append(
                {
                    "id": trace.get("id", path.stem),
                    "created_at": trace.get("created_at", ""),
                    "tool": trace.get("tool", ""),
                    "ok": trace.get("ok", False),
                    "blocked": trace.get("blocked", False),
                    "subject": trace.get("policy", {}).get("subject", ""),
                }
            )
        return sorted(items, key=lambda item: item["created_at"], reverse=True)

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        path = self.workspace.tool_traces_dir / f"{trace_id}.json"
        if not path.exists():
            raise KeyError(f"Unknown tool trace: {trace_id}")
        return Workspace.read_json(path)

    def _write_trace(self, result: dict[str, Any]) -> dict[str, Any]:
        self.workspace.tool_traces_dir.mkdir(parents=True, exist_ok=True)
        Workspace.write_json(self.workspace.tool_traces_dir / f"{result['id']}.json", result)
        return result


def tool_result(
    tool: str,
    policy: dict[str, Any],
    *,
    ok: bool,
    output: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid4().hex[:12],
        "created_at": utc_now(),
        "tool": tool,
        "ok": ok,
        "blocked": False,
        "policy": policy,
        "output": output or {},
        "error": error,
    }


def blocked_result(tool: str, policy: dict[str, Any], *, output: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": uuid4().hex[:12],
        "created_at": utc_now(),
        "tool": tool,
        "ok": False,
        "blocked": True,
        "policy": policy,
        "output": output or {},
        "error": "blocked by policy",
    }


def split_command(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def trim_capture(text: str) -> str:
    return text[:MAX_CAPTURE_CHARS]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(text)
    try:
        temp_path.replace(path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def atomic_create_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(text)
    try:
        os.link(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def is_relative_path(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def utc_now_from_timestamp(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def normalize_patch_edit(edit: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(edit, dict):
        raise ValueError("Batch patch edits must be objects.")
    path = edit.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Batch patch edit requires a non-empty path.")
    normalized: dict[str, Any] = {
        "path": path.strip(),
        "new": str(edit.get("new", "")),
    }
    for key in ["old", "start_anchor", "end_anchor"]:
        value = edit.get(key)
        if value is not None:
            normalized[key] = str(value)
    if edit.get("replace_all"):
        normalized["replace_all"] = True
    if edit.get("occurrence") not in {None, ""}:
        normalized["occurrence"] = int(edit["occurrence"])
    if edit.get("include_anchors"):
        normalized["include_anchors"] = True
    return normalized


def batch_child_summary(index: int, result: dict[str, Any]) -> dict[str, Any]:
    output = result.get("output", {})
    summary = {
        "index": index,
        "trace_id": result["id"],
        "tool": result["tool"],
        "ok": result["ok"],
        "blocked": result["blocked"],
        "path": output.get("path") or result.get("policy", {}).get("subject", ""),
        "mode": output.get("mode"),
        "replacement_count": output.get("replacement_count"),
        "error": result.get("error"),
        "policy_status": result.get("policy", {}).get("status"),
        "policy_reasons": result.get("policy", {}).get("reasons", []),
    }
    if result["blocked"] or not result["ok"]:
        summary["failure_reason"] = child_failure_reason(result)
    return summary


def child_failure_reason(result: dict[str, Any]) -> str:
    if result.get("error"):
        return str(result["error"])
    output = result.get("output", {})
    if result.get("tool") == "run_command" and isinstance(output, dict) and output.get("exit_code") is not None:
        return f"run_command exit_code={output.get('exit_code')}"
    return str(result.get("policy", {}).get("status") or "failed")


def normalize_transaction_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not steps:
        raise ValueError("transaction requires a non-empty steps array")
    normalized: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"transaction step {index} must be an object")
        action = normalize_transaction_action(str(step.get("action") or step.get("tool") or step.get("name") or ""))
        if action not in TRANSACTION_ACTIONS:
            raise ValueError(f"transaction step {index} has unsupported action: {action or '[missing]'}")
        if action in {"create_file", "write_file"}:
            normalized.append(
                {
                    "action": action,
                    "path": require_transaction_string(step, "path", index),
                    "text": str(step.get("text", "")),
                }
            )
        elif action == "append_file":
            normalized.append(
                {
                    "action": "append_file",
                    "path": require_transaction_string(step, "path", index),
                    "text": str(step.get("text", "")),
                    "create": bool(step.get("create", True)),
                }
            )
        elif action == "patch_file":
            normalized.append({"action": "patch_file", **normalize_patch_edit(step)})
        else:
            normalized.append(
                {
                    "action": "run_command",
                    "command": require_transaction_string(step, "command", index),
                    "timeout_seconds": clamp_transaction_int(
                        step.get("timeout_seconds", step.get("timeout", 30)),
                        default=30,
                        minimum=1,
                        maximum=300,
                    ),
                }
            )
    return normalized


def normalize_transaction_action(action: str) -> str:
    normalized = action.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "create": "create_file",
        "write": "write_file",
        "append": "append_file",
        "patch": "patch_file",
        "edit_file": "patch_file",
        "run": "run_command",
        "exec": "run_command",
        "shell": "run_command",
        "command": "run_command",
    }
    return aliases.get(normalized, normalized)


def require_transaction_string(step: dict[str, Any], key: str, index: int) -> str:
    value = step.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"transaction step {index} requires a non-empty {key}")
    return value.strip()


def clamp_transaction_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def transaction_file_paths(steps: list[dict[str, Any]]) -> list[str]:
    return [step["path"] for step in steps if step["action"] in TRANSACTION_FILE_ACTIONS]


def transaction_command_policies(
    workspace: Workspace,
    steps: list[dict[str, Any]],
    *,
    allow_destructive_commands: bool,
) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if step["action"] != "run_command":
            continue
        policies.append(
            {
                "step": index,
                "command": step["command"],
                "root": command_root(step["command"]),
                "policy": check_command_policy(
                    step["command"],
                    workspace=workspace,
                    allow_destructive=allow_destructive_commands,
                ),
            }
        )
    return policies


def transaction_command_policy(command_policies: list[dict[str, Any]]) -> dict[str, Any] | None:
    blocked = [item for item in command_policies if not item["policy"]["allowed"]]
    if not blocked:
        return None
    reasons: list[str] = []
    subjects: list[str] = []
    for item in blocked:
        subjects.append(str(item["policy"].get("subject") or item["command"]))
        reasons.extend([f"step {item['step']}: {reason}" for reason in item["policy"].get("reasons", [])])
    return {
        "allowed": False,
        "status": "blocked",
        "kind": "transaction_command",
        "operation": "transaction",
        "subject": "; ".join(subjects),
        "reasons": reasons or ["transaction contains a blocked command step"],
        "items": command_policies,
    }


def transaction_safety_summary(
    workspace: Workspace,
    steps: list[dict[str, Any]],
    *,
    command_policies: list[dict[str, Any]],
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    creates = 0
    modifies = 0
    overwrites = 0
    missing_inputs = 0
    for index, step in enumerate(steps, start=1):
        action = step["action"]
        if action in TRANSACTION_FILE_ACTIONS:
            target = resolve_target(workspace, Path(step["path"]))
            exists = target.exists()
            effect = transaction_file_effect(action, exists, create=bool(step.get("create", True)))
            if effect == "create":
                creates += 1
            if effect in {"modify", "overwrite"}:
                modifies += 1
            if effect == "overwrite":
                overwrites += 1
            if action == "patch_file" and not exists:
                missing_inputs += 1
            files.append(
                {
                    "step": index,
                    "action": action,
                    "path": step["path"],
                    "exists": exists,
                    "effect": effect,
                }
            )
    for item in command_policies:
        policy = item["policy"]
        commands.append(
            {
                "step": item["step"],
                "command": item["command"],
                "root": item["root"],
                "allowed": policy["allowed"],
                "reasons": policy.get("reasons", []),
            }
        )
    return {
        "step_count": len(steps),
        "file_step_count": len(files),
        "command_step_count": len(commands),
        "creates": creates,
        "modifies": modifies,
        "overwrites": overwrites,
        "missing_inputs": missing_inputs,
        "has_verification": bool(commands),
        "files": files,
        "commands": commands,
    }


def transaction_file_effect(action: str, exists: bool, *, create: bool) -> str:
    if action == "create_file":
        return "conflict" if exists else "create"
    if action == "write_file":
        return "overwrite" if exists else "create"
    if action == "append_file":
        if exists:
            return "modify"
        return "create" if create else "missing"
    if action == "patch_file":
        return "modify" if exists else "missing"
    return "unknown"


def transaction_preflight_failure(kind: str, policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": "preflight",
        "kind": kind,
        "blocked": True,
        "reason": "; ".join(str(reason) for reason in policy.get("reasons", [])) or str(policy.get("status") or "blocked"),
        "policy_status": policy.get("status"),
        "policy_subject": policy.get("subject"),
    }


def transaction_step_failure(index: int, step: dict[str, Any], result: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "stage": "execution",
        "step": index,
        "action": step["action"],
        "tool": result.get("tool"),
        "trace_id": result.get("id"),
        "blocked": bool(result.get("blocked")),
        "reason": reason,
        "policy_status": result.get("policy", {}).get("status"),
        "policy_subject": result.get("policy", {}).get("subject"),
    }


def transaction_policy(workspace: Workspace, file_paths: list[str]) -> dict[str, Any]:
    if not file_paths:
        return {
            "allowed": True,
            "status": "allowed",
            "kind": "transaction",
            "operation": "transaction",
            "subject": "commands only",
            "reasons": [],
        }
    policy = check_batch_file_policy(workspace, [{"path": path} for path in file_paths])
    policy["operation"] = "transaction"
    return policy


def replace_nth_occurrence(text: str, old: str, new: str, occurrence: int) -> str:
    start = 0
    index = -1
    for _ in range(occurrence):
        index = text.find(old, start)
        if index < 0:
            return text
        start = index + len(old)
    return text[:index] + new + text[index + len(old) :]


def find_anchor_blocks(
    text: str,
    start_anchor: str,
    end_anchor: str,
    *,
    include_anchors: bool,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    search_from = 0
    while True:
        start_index = text.find(start_anchor, search_from)
        if start_index < 0:
            break
        end_index = text.find(end_anchor, start_index + len(start_anchor))
        if end_index < 0:
            break
        replace_start = start_index if include_anchors else start_index + len(start_anchor)
        replace_end = end_index + len(end_anchor) if include_anchors else end_index
        matches.append(
            {
                "replace_start": replace_start,
                "replace_end": replace_end,
                "original": text[replace_start:replace_end],
            }
        )
        search_from = end_index + len(end_anchor)
    return matches


def normalize_block_replacement(new: str, original: str) -> str:
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
