from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .cli_output import print_json
from .cli_reports import print_tool_result
from .storage import Workspace
from .tasks import TaskStore
from .tools import ToolExecutor


DEFAULT_STREAM_PREVIEW_CHARS = 1200


def add_tool_subcommands(tool_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    tool_read = tool_sub.add_parser("read", help="Read a workspace file through policy.")
    tool_read.add_argument("path")
    tool_read.add_argument("--max-chars", type=int, default=8000)
    tool_read.add_argument("--json", action="store_true")
    tool_read.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_read.set_defaults(func=cmd_tool_read)

    tool_list_dir = tool_sub.add_parser("list-dir", help="List a workspace directory through policy.")
    tool_list_dir.add_argument("path", nargs="?", default=".")
    tool_list_dir.add_argument("--limit", type=int, default=200)
    tool_list_dir.add_argument("--json", action="store_true")
    tool_list_dir.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_list_dir.set_defaults(func=cmd_tool_list_dir)

    tool_file_info = tool_sub.add_parser("file-info", help="Show workspace file metadata through policy.")
    tool_file_info.add_argument("path")
    tool_file_info.add_argument("--json", action="store_true")
    tool_file_info.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_file_info.set_defaults(func=cmd_tool_file_info)

    tool_create = tool_sub.add_parser("create", help="Create a workspace file without overwriting existing files.")
    tool_create.add_argument("path")
    tool_create.add_argument("--text", required=True)
    tool_create.add_argument("--json", action="store_true")
    tool_create.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_create.set_defaults(func=cmd_tool_create)

    tool_write = tool_sub.add_parser("write", help="Write a workspace file through policy.")
    tool_write.add_argument("path")
    tool_write.add_argument("--text", required=True)
    tool_write.add_argument("--json", action="store_true")
    tool_write.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_write.set_defaults(func=cmd_tool_write)

    tool_append = tool_sub.add_parser("append", help="Append text to a workspace file through policy.")
    tool_append.add_argument("path")
    tool_append.add_argument("--text", required=True)
    tool_append.add_argument("--no-create", action="store_true", help="Fail if the target file does not already exist.")
    tool_append.add_argument("--json", action="store_true")
    tool_append.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_append.set_defaults(func=cmd_tool_append)

    tool_patch = tool_sub.add_parser("patch", help="Patch a workspace file with structured replacement modes.")
    tool_patch.add_argument("path")
    tool_patch.add_argument("--old")
    tool_patch.add_argument("--new", required=True)
    tool_patch.add_argument("--replace-all", action="store_true", help="Replace every match of --old instead of requiring a single match.")
    tool_patch.add_argument("--occurrence", type=int, default=None, help="Replace only the nth match of --old.")
    tool_patch.add_argument("--start-anchor", default=None, help="Replace the block that starts after this anchor.")
    tool_patch.add_argument("--end-anchor", default=None, help="Replace the block that ends before this anchor.")
    tool_patch.add_argument("--include-anchors", action="store_true", help="Replace the anchors together with the block body.")
    tool_patch.add_argument("--json", action="store_true")
    tool_patch.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_patch.set_defaults(func=cmd_tool_patch)

    tool_batch_patch = tool_sub.add_parser("batch-patch", help="Apply multiple structured patches from a JSON spec file.")
    tool_batch_patch.add_argument("--specs-file", required=True, help="JSON file containing an array of patch specs, or an object with an edits array.")
    tool_batch_patch.add_argument("--json", action="store_true")
    tool_batch_patch.add_argument("--task", default=None, help="Attach the batch trace to a task session.")
    tool_batch_patch.set_defaults(func=cmd_tool_batch_patch)

    tool_delete = tool_sub.add_parser("delete", help="Delete a workspace file through destructive policy.")
    tool_delete.add_argument("path")
    tool_delete.add_argument("--allow-destructive", action="store_true")
    tool_delete.add_argument("--json", action="store_true")
    tool_delete.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_delete.set_defaults(func=cmd_tool_delete)

    tool_exec = tool_sub.add_parser("exec", help="Run a safe command through policy.")
    tool_exec.add_argument("--allow-destructive", action="store_true")
    tool_exec.add_argument("--timeout", type=int, default=30)
    tool_exec.add_argument("--full-output", action="store_true", help="Print full captured stdout/stderr instead of a folded preview.")
    tool_exec.add_argument("--json", action="store_true")
    tool_exec.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_exec.add_argument("command", nargs=argparse.REMAINDER)
    tool_exec.set_defaults(func=cmd_tool_exec)

    tool_list = tool_sub.add_parser("list", help="List tool execution traces.")
    tool_list.set_defaults(func=cmd_tool_list)

    tool_show = tool_sub.add_parser("show", help="Show a tool execution trace.")
    tool_show.add_argument("trace_id")
    tool_show.set_defaults(func=cmd_tool_show)


def cmd_tool_read(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).read_file(args.path, max_chars=args.max_chars)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)
    if result["ok"]:
        print(result["output"]["content"])


def cmd_tool_list_dir(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).list_dir(args.path, limit=args.limit)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)
    if result["ok"]:
        output = result.get("output", {})
        for entry in output.get("entries", []):
            size = "" if entry.get("size_bytes") is None else f"\t{entry['size_bytes']} bytes"
            print(f"{entry['kind']}\t{entry['path']}{size}")
        if output.get("truncated"):
            print(f"truncated: showing {len(output.get('entries', []))}/{output.get('total_entries', 0)}")


def cmd_tool_file_info(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).file_info(args.path)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)
    if result["ok"]:
        output = result.get("output", {})
        if not output.get("exists"):
            print("exists: false")
            print(f"path: {output.get('path')}")
            return
        print(f"path: {output.get('path')}")
        print(f"kind: {output.get('kind')}")
        if output.get("size_bytes") is not None:
            print(f"size_bytes: {output.get('size_bytes')}")
        if output.get("modified_at"):
            print(f"modified_at: {output.get('modified_at')}")


def cmd_tool_create(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).create_file(args.path, args.text)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)


def cmd_tool_write(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).write_file(args.path, args.text)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)


def cmd_tool_append(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).append_file(args.path, args.text, create=not args.no_create)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)


def cmd_tool_patch(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    if (args.start_anchor or args.end_anchor) and args.old:
        raise ValueError("tool patch cannot combine --old with --start-anchor/--end-anchor")
    if not (args.start_anchor or args.end_anchor) and not args.old:
        raise ValueError("tool patch requires --old, or both --start-anchor and --end-anchor")
    result = ToolExecutor(workspace).patch_file(
        args.path,
        args.old or "",
        args.new,
        replace_all=args.replace_all,
        occurrence=args.occurrence,
        start_anchor=args.start_anchor,
        end_anchor=args.end_anchor,
        include_anchors=args.include_anchors,
    )
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)


def cmd_tool_batch_patch(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    edits = load_batch_patch_specs(Path(args.specs_file))
    result = ToolExecutor(workspace).batch_patch(edits)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)
    output = result.get("output", {})
    if "applied_count" in output:
        print(f"applied_count: {output['applied_count']}")
    transaction = output.get("transaction", {})
    if isinstance(transaction, dict) and transaction:
        print(
            "transaction: "
            f"{transaction.get('id')} "
            f"{transaction.get('status')} "
            f"snapshots={transaction.get('snapshot_count', 0)}"
        )
        rollback = transaction.get("rollback", {})
        if isinstance(rollback, dict) and rollback:
            print(
                "rollback: "
                f"restored={len(rollback.get('restored', []))} "
                f"deleted={len(rollback.get('deleted', []))}"
            )
    if output.get("rolled_back"):
        print("rolled_back: true")


def load_batch_patch_specs(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    edits = payload.get("edits") if isinstance(payload, dict) else payload
    if not isinstance(edits, list):
        raise ValueError("batch-patch specs file must contain a JSON array or an object with an `edits` array.")
    if not all(isinstance(edit, dict) for edit in edits):
        raise ValueError("batch-patch edits must be JSON objects.")
    return edits


def cmd_tool_delete(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).delete_file(args.path, allow_destructive=args.allow_destructive)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)


def cmd_tool_exec(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    result = ToolExecutor(workspace).run_command(
        " ".join(command),
        allow_destructive=args.allow_destructive,
        timeout_seconds=args.timeout,
    )
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)
    output = result.get("output", {})
    if "exit_code" in output:
        print(f"exit_code: {output['exit_code']}")
    if output.get("stdout"):
        print_command_stream("stdout", output["stdout"], trace_id=result["id"], full=args.full_output)
    if output.get("stderr"):
        print_command_stream("stderr", output["stderr"], trace_id=result["id"], full=args.full_output)


def cmd_tool_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    traces = ToolExecutor(workspace).list_traces()
    if not traces:
        print("no tool traces")
        return
    for trace in traces:
        status = "blocked" if trace["blocked"] else "ok" if trace["ok"] else "failed"
        print(f"{trace['id']}\t{trace['created_at']}\t{trace['tool']}\t{status}\t{trace['subject']}")


def cmd_tool_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    print_json(ToolExecutor(workspace).get_trace(args.trace_id))


def workspace_from_args(args: argparse.Namespace) -> Workspace:
    workspace = Workspace(Path(args.workspace))
    workspace.require_initialized()
    return workspace


def ensure_task_attachable(workspace: Workspace, task_id: str | None) -> None:
    if not task_id:
        return
    task = TaskStore(workspace).get(task_id)
    if task.get("status") == "completed":
        raise ValueError(f"Task is completed and cannot receive new traces: {task_id}")


def attach_tool_to_task_if_requested(workspace: Workspace, task_id: str | None, result: dict[str, Any]) -> None:
    if not task_id:
        return
    TaskStore(workspace).attach(task_id, "tool", result["id"])


def print_command_stream(
    label: str,
    text: str,
    *,
    trace_id: str,
    full: bool = False,
    preview_chars: int = DEFAULT_STREAM_PREVIEW_CHARS,
) -> None:
    print(f"{label}:")
    clean = text.rstrip()
    if full or len(clean) <= preview_chars:
        print(clean)
        return
    head_chars = max(200, int(preview_chars * 0.7))
    tail_chars = max(120, preview_chars - head_chars)
    print(clean[:head_chars].rstrip())
    omitted = len(clean) - head_chars - tail_chars
    if omitted > 0:
        print(f"... {label} folded: omitted {omitted} chars; full output is in tool trace {trace_id}")
        print(f"... inspect with: akernel tool show {trace_id}")
    print(clean[-tail_chars:].lstrip())
