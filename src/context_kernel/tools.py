from __future__ import annotations

import shlex
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import utc_now
from .policy import check_batch_file_policy, check_command_policy, check_file_policy, resolve_target
from .storage import Workspace
from .tool_transactions import begin_file_transaction


MAX_CAPTURE_CHARS = 8000
MAX_LIST_ENTRIES = 200


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


def blocked_result(tool: str, policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": uuid4().hex[:12],
        "created_at": utc_now(),
        "tool": tool,
        "ok": False,
        "blocked": True,
        "policy": policy,
        "output": {},
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
    return {
        "index": index,
        "trace_id": result["id"],
        "tool": result["tool"],
        "ok": result["ok"],
        "blocked": result["blocked"],
        "path": output.get("path") or result.get("policy", {}).get("subject", ""),
        "mode": output.get("mode"),
        "replacement_count": output.get("replacement_count"),
        "error": result.get("error"),
    }


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
