from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from .loop import summarize_tool_result
from .storage import Workspace
from .tasks import TaskStore
from .terminal import chat_color, chat_notice, chat_panel, wrap_chat_text
from .tools import ToolExecutor


INLINE_FILE_REF_RE = re.compile(r"(?<![\w@])@([^\s,;:]+)")

IGNORED_FILE_FINDER_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".akernel",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
}


def load_custom_chat_commands(root: Path) -> dict[str, dict[str, str]]:
    commands: dict[str, dict[str, str]] = {}
    for directory, scope in custom_command_directories(root):
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.md"))[:80]:
            try:
                relative = path.relative_to(directory).with_suffix("").as_posix()
                body = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            name = "/" + relative.strip("/")
            if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_\-/]*", name):
                continue
            description, prompt = parse_custom_command(body)
            commands.setdefault(
                name,
                {
                    "description": description or first_non_empty_line(prompt) or "run saved prompt",
                    "path": str(path),
                    "prompt": prompt,
                    "scope": scope,
                },
            )
    return commands


def custom_command_directories(root: Path) -> list[tuple[Path, str]]:
    return [
        (root / ".akernel" / "commands", "project"),
        (Path.home() / ".akernel" / "commands", "user"),
    ]


def parse_custom_command(text: str) -> tuple[str, str]:
    description = ""
    body = text.strip()
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            meta = parts[1]
            body = parts[2].strip()
            for line in meta.splitlines():
                key, _, value = line.partition(":")
                if key.strip().casefold() == "description":
                    description = value.strip().strip('"').strip("'")
                    break
    return description, body


def first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        value = line.strip().lstrip("#").strip()
        if value:
            return value[:80]
    return ""


def print_custom_commands_panel(root: Path) -> None:
    commands = load_custom_chat_commands(root)
    if not commands:
        chat_notice(
            "Slash Commands",
            "No custom commands yet. Add Markdown prompts under .akernel/commands or ~/.akernel/commands.",
        )
        return
    chat_panel(
        "Slash Commands",
        [
            (name, f"{spec['description']} ({spec['scope']})")
            for name, spec in sorted(commands.items())
        ],
    )


def expand_custom_chat_command(root: Path, request: str) -> str | None:
    command, _, arguments = request.strip().partition(" ")
    if not command.startswith("/"):
        return None
    spec = load_custom_chat_commands(root).get(command)
    if not spec:
        return None
    prompt = spec["prompt"]
    if not prompt:
        return arguments.strip() or f"Run custom command {command}."
    arguments = arguments.strip()
    return (
        prompt.replace("{{args}}", arguments)
        .replace("{{arguments}}", arguments)
        .replace("$ARGUMENTS", arguments)
        .strip()
    )


def attach_inline_file_references(
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    request: str,
    pending_context: list[str],
) -> int:
    attached = 0
    seen: set[str] = set()
    for match in INLINE_FILE_REF_RE.finditer(request):
        path = match.group(1).strip("`'\".,;:!?)]}")
        if not path or path.isdigit() or path in seen:
            continue
        seen.add(path)
        if (workspace.root / path).is_file():
            attach_chat_file(workspace, tasks, task_id, path, pending_context)
            attached += 1
    return attached


def attach_chat_file_command(
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    query: str,
    pending_context: list[str],
    state: dict[str, Any] | None = None,
) -> None:
    state = state if state is not None else {}
    query = query.strip().strip('"').strip("'")
    if query.isdigit() and state.get("file_matches"):
        matches = list(state.get("file_matches") or [])
        index = int(query) - 1
        if 0 <= index < len(matches):
            attach_chat_file(workspace, tasks, task_id, str(matches[index]), pending_context)
            return
        chat_notice("File Search", f"No cached match @{query}. Use @ to list files again.")
        return

    if query and (workspace.root / query).is_file():
        attach_chat_file(workspace, tasks, task_id, query, pending_context)
        return

    matches = find_workspace_files(workspace.root, query, limit=12)
    state["file_matches"] = matches
    if not matches:
        hint = "Try @readme, @pyproject, or a filename fragment."
        chat_notice("File Search", f"No files matched `{query or '*'}`. {hint}")
        return
    if query and len(matches) == 1:
        attach_chat_file(workspace, tasks, task_id, matches[0], pending_context)
        return

    print("")
    print(chat_color("[ File Search ]", "cyan", bold=True))
    print(wrap_chat_text("Type @1, @2, ... to attach a result, or keep typing a narrower @query.", indent="  "))
    for index, path in enumerate(matches, start=1):
        print(f"  @{index:<2} {path}")


def find_workspace_files(root: Path, query: str, *, limit: int = 12, max_scan: int = 2500) -> list[str]:
    normalized_query = query.casefold().replace("\\", "/")
    candidates: list[tuple[int, str]] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_FILE_FINDER_DIRS and not name.startswith(".mypy_cache")
        ]
        for filename in filenames:
            scanned += 1
            if scanned > max_scan:
                break
            path = Path(dirpath) / filename
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            haystack = relative.casefold()
            name = filename.casefold()
            if normalized_query and normalized_query not in haystack:
                continue
            score = 0
            if normalized_query:
                if name == normalized_query:
                    score -= 40
                elif name.startswith(normalized_query):
                    score -= 25
                elif haystack.startswith(normalized_query):
                    score -= 15
                score += haystack.find(normalized_query)
            score += relative.count("/") * 2
            score += len(relative) // 20
            candidates.append((score, relative))
        if scanned > max_scan:
            break
    return [path for _, path in sorted(candidates, key=lambda item: (item[0], item[1]))[:limit]]


def attach_chat_file(
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    path: str,
    pending_context: list[str],
) -> None:
    if not path:
        chat_notice("Attach File", "Usage: @relative/path.txt")
        return
    result = ToolExecutor(workspace).read_file(path)
    tasks.attach(task_id, "tool", result["id"])
    summary = summarize_tool_result(result)
    tasks.step(
        task_id,
        f"User attached file {path}: {summary}",
        kind="chat_file",
        refs={"tool_traces": [result["id"]]},
    )
    if result["ok"] and not result["blocked"]:
        output = result.get("output", {})
        content = str(output.get("content", ""))
        pending_context.append(
            f"Attached file `{path}` ({output.get('size_chars', len(content))} chars, "
            f"truncated={bool(output.get('truncated'))}):\n{content}"
        )
        chat_notice("Attached File", f"{path} is attached to the next task.")
    else:
        chat_notice("Attach Failed", summary)


def run_chat_command(
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    command: str,
    pending_context: list[str],
) -> None:
    if not command:
        chat_notice("Command", "Usage: !python -c \"print(123)\"")
        return
    result = ToolExecutor(workspace).run_command(command)
    tasks.attach(task_id, "tool", result["id"])
    summary = summarize_tool_result(result)
    tasks.step(
        task_id,
        f"User ran command `{command}`: {summary}",
        kind="chat_command",
        refs={"tool_traces": [result["id"]]},
    )
    output = result.get("output", {})
    pending_context.append(
        "Command result attached to the next task:\n"
        f"command: {command}\n"
        f"ok: {result.get('ok')}\n"
        f"blocked: {result.get('blocked')}\n"
        f"summary: {summary}\n"
        f"stdout: {str(output.get('stdout', ''))[:1200]}\n"
        f"stderr: {str(output.get('stderr', ''))[:800]}"
    )
    title = "Command Complete" if result.get("ok") else "Command Blocked" if result.get("blocked") else "Command Failed"
    chat_notice(title, summary)
