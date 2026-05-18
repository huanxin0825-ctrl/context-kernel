from __future__ import annotations

import argparse
from pathlib import Path
import shutil
from typing import Any

from .budget import DEFAULT_PROFILE
from .chat_extensions import extension_summary
from .chat_ui import report_budget_total, tui_flow_summary, tui_status_bar, tui_timeline_row, tui_token_meter
from .model_config import auxiliary_model, primary_model
from .storage import Workspace
from .tasks import TaskStore
from .terminal import (
    chat_color,
    chat_width,
    compact_path,
    pad_display,
    truncate_line,
    tui_pill,
    tui_rule,
    tui_soft_rule,
    wrap_plain,
)
from .workspace_summary import workspace_state_summary


def tui_prompt(args: argparse.Namespace) -> str:
    return chat_color("\nakernel", "cyan", bold=True) + chat_color(f" // {primary_model(args)}", "dim") + " > "


def render_chat_tui_screen(
    workspace: Workspace,
    task_id: str,
    args: argparse.Namespace,
    transcript: list[dict[str, str]],
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    *,
    status: str,
    state: dict[str, Any] | None = None,
    clear: bool = True,
) -> None:
    screen = build_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status=status, state=state)
    prefix = "\033[2J\033[H" if clear else "\n"
    print(prefix + screen + ("\n" if not clear else ""), end="")
    if state is not None:
        state["rendered_count"] = len(transcript)


def render_chat_tui_update(
    workspace: Workspace,
    task_id: str,
    args: argparse.Namespace,
    transcript: list[dict[str, str]],
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    *,
    status: str,
    state: dict[str, Any] | None = None,
    clear: bool = True,
) -> None:
    if clear:
        render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status=status, state=state, clear=True)
        return
    start = int((state or {}).get("rendered_count", max(0, len(transcript) - 1)))
    for item in transcript[start:]:
        render_chat_tui_message(item)
    if state is not None:
        state["rendered_count"] = len(transcript)
    render_chat_tui_status(status, args, pending_context, last_report=last_report)


def render_chat_tui_message(item: dict[str, str]) -> None:
    width = chat_width()
    role = item.get("role", "system")
    title = item.get("title", role)
    label = tui_role_label(role, title)
    prefix = "  " if role != "user" else "> "
    print("")
    print(chat_color(truncate_line(label, width), "cyan" if role == "system" else "green" if role == "assistant" else "yellow", bold=True))
    for line in wrap_plain(item.get("text", ""), width=max(20, width - len(prefix))).splitlines():
        print(truncate_line(prefix + line, width))


def render_chat_tui_status(
    status: str,
    args: argparse.Namespace,
    pending_context: list[str],
    *,
    last_report: dict[str, Any] | None = None,
) -> None:
    if last_report:
        tokens = last_report.get("totals", {}).get("total_tokens", 0)
        run_id = str(last_report.get("id", ""))[:12]
        run_status = str(last_report.get("status", status))
        summary = f"{run_status} | {tokens} tokens | run {run_id} | /cost"
    elif status == "running":
        attached = f" | ctx {len(pending_context)}" if pending_context else ""
        summary = f"running | primary {primary_model(args)} | route {getattr(args, 'model_routing', 'primary')}{attached}"
    else:
        attached = f" | ctx {len(pending_context)}" if pending_context else ""
        summary = f"{status} | primary {primary_model(args)} | route {getattr(args, 'model_routing', 'primary')}{attached} | / commands | @ files"
    print(chat_color(truncate_line(summary, chat_width()), "dim"), flush=True)


def build_chat_tui_screen(
    workspace: Workspace,
    task_id: str,
    args: argparse.Namespace,
    transcript: list[dict[str, str]],
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    *,
    status: str,
    state: dict[str, Any] | None = None,
) -> str:
    width = chat_width()
    if (state or {}).get("scrollback_mode"):
        return "\n".join(tui_compact_start_lines(workspace, task_id, args, last_report, pending_context, status=status, width=width))
    height = max(24, shutil.get_terminal_size((width, 32)).lines)
    right_width = min(40, max(32, width // 3))
    left_width = max(46, width - right_width - 3)
    header = tui_header_lines(workspace, args, last_report, pending_context, status=status, width=width)
    footer = tui_footer_lines(width)
    body_height = max(10, height - len(header) - len(footer) - 1)
    lines = header
    body = tui_body_lines(transcript, left_width, status=status)
    side = tui_sidebar_lines(workspace, task_id, args, last_report, pending_context, right_width)
    scroll_offset = max(0, int((state or {}).get("scroll_offset", 0)))
    body = slice_tui_body(body, body_height, scroll_offset, left_width)
    side = side[:body_height]
    for index in range(body_height):
        left = body[index] if index < len(body) else ""
        right = side[index] if index < len(side) else ""
        lines.append(f"{pad_display(left, left_width)} {chat_color('|', 'dim')} {pad_display(right, right_width)}")
    lines.extend(footer)
    return "\n".join(lines)


def tui_compact_start_lines(
    workspace: Workspace,
    task_id: str,
    args: argparse.Namespace,
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    *,
    status: str,
    width: int,
) -> list[str]:
    tokens = 0 if not last_report else last_report.get("totals", {}).get("total_tokens", 0)
    task_short = task_id[:12]
    route = getattr(args, "model_routing", "primary")
    return [
        "",
        chat_color(truncate_line("akernel", width), "cyan", bold=True),
        chat_color(truncate_line("focused agent workspace", width), "dim"),
        "",
        truncate_line(f"{compact_path(Path.cwd())}", width),
        chat_color(truncate_line(f"task {task_short} | {args.provider} | {primary_model(args)} | {args.profile} | route {route}", width), "dim"),
        "",
        truncate_line("Ask a task, attach @file, or run !command for checked local context.", width),
        chat_color(truncate_line(f"/help commands | /status session | /extensions tools | /cost last run | ctx {len(pending_context)} | last {tokens} tokens", width), "dim"),
    ]


def tui_header_lines(
    workspace: Workspace,
    args: argparse.Namespace,
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    *,
    status: str,
    width: int,
) -> list[str]:
    status_label = status.upper()
    status_color = "green" if status == "ready" else "yellow" if status == "running" else "cyan"
    tokens = 0 if not last_report else last_report.get("totals", {}).get("total_tokens", 0)
    run_id = str(last_report.get("id", ""))[:12] if last_report else "none"
    title = f" AKERNEL // {status_label} "
    subtitle = (
        f"{compact_path(workspace.root)}  |  provider {args.provider}  |  "
        f"profile {getattr(args, 'profile', DEFAULT_PROFILE)}  |  run {run_id}"
    )
    model_line = (
        f"primary {primary_model(args)}  |  aux {auxiliary_model(args)}  |  "
        f"route {getattr(args, 'model_routing', 'primary')}  |  tokens {tokens}"
    )
    status_line = (
        f"{tui_status_bar(status, width)}  "
        f"ctx {0 if last_report is None else len(last_report.get('steps', []))} step(s)  |  "
        f"{tui_flow_summary(last_report)}"
    )
    return [
        chat_color(tui_rule(title, width), status_color, bold=True),
        truncate_line(subtitle, width),
        chat_color(truncate_line(model_line, width), "dim"),
        chat_color(truncate_line(status_line, width), "dim"),
        tui_command_strip(width),
    ]


def tui_footer_lines(width: int) -> list[str]:
    return [
        tui_soft_rule(" Input ", width),
        truncate_line("Ask one concrete task. @ finds files, @1 attaches a match, !cmd captures checked shell context.", width),
        chat_color(truncate_line("/up and /down move transcript view, /latest returns to now.", width), "dim"),
        "",
    ]


def tui_command_strip(width: int) -> str:
    commands = " /help  /status  /model  /extensions  /commands  /compact  /runs  /cost  @file  !cmd "
    return chat_color(truncate_line(commands.center(width, "-"), width), "dim")


def tui_body_lines(transcript: list[dict[str, str]], width: int, *, status: str = "ready") -> list[str]:
    if not transcript:
        return [
            "Start",
            "-" * min(width, 32),
            "",
            "Ask one focused task.",
            "Attach context with @path.",
            "Capture safe shell output with !command.",
            "",
            "Details stay available through slash commands.",
        ]
    lines: list[str] = []
    lines.append(f"Conversation [{status}]")
    lines.append("=" * min(width, 32))
    for item in transcript:
        title = item.get("title", item.get("role", "message"))
        role = item.get("role", "system")
        label = tui_role_label(role, title)
        lines.append("")
        lines.append(truncate_line(tui_pill(label, width), width))
        prefix = "  " if role != "user" else "> "
        for line in wrap_plain(item.get("text", ""), width=max(20, width - len(prefix))).splitlines():
            lines.append(truncate_line(prefix + line, width))
    return lines


def tui_role_label(role: str, title: str) -> str:
    labels = {
        "user": "YOU",
        "assistant": "AKERNEL",
        "system": "SYSTEM",
    }
    base = labels.get(role, role.upper())
    if role == "assistant" and title.casefold() in {"assistant", "akernel"}:
        return base
    return f"{base}: {title}" if title and title.casefold() != base.casefold() else base


def tui_sidebar_lines(
    workspace: Workspace,
    task_id: str,
    args: argparse.Namespace,
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    width: int,
) -> list[str]:
    rows = tui_section("Focus", width)
    rows.append("small context, visible actions")
    rows.append("quiet by default, inspectable on demand")
    rows.extend(tui_flow_panel(last_report, width))
    rows.extend(tui_section("Session", width))
    rows.extend(
        [
            tui_kv("provider", args.provider, width),
            tui_kv("profile", f"{getattr(args, 'profile', DEFAULT_PROFILE)} | route {getattr(args, 'model_routing', 'auto')}", width),
            tui_kv("context", f"{len(pending_context)} attached", width),
            tui_kv("extend", compact_extension_label(workspace), width),
        ]
    )
    if last_report:
        rows.extend(tui_last_run_panel(last_report, width))
    else:
        rows.extend(tui_section("Next", width))
        rows.extend(
            [
                tui_kv("attach", "@query or @path", width),
                tui_kv("shell", "!command", width),
                tui_kv("brief", "/compact", width),
            ]
        )
    rows.extend(tui_task_panel(workspace, task_id, width))
    rows.extend(tui_section("Models", width))
    rows.extend(
        [
            tui_kv("primary", primary_model(args), width),
            tui_kv("aux", auxiliary_model(args), width),
            tui_kv("review", getattr(args, "aux_review", "auto"), width),
            "",
        ]
    )
    if last_report:
        diagnostic = last_report.get("diagnostic")
        if isinstance(diagnostic, dict) and diagnostic:
            rows.extend([""])
            rows.extend(tui_section("Diagnostic", width))
            rows.append(str(diagnostic.get("category", "")))
            rows.extend(wrap_plain(str(diagnostic.get("suggestion", "")), width=max(20, width)).splitlines())
        rows.append("")
    rows.extend(tui_section("Workspace", width))
    rows.extend(wrap_plain(workspace_state_summary(workspace), width=max(20, width)).splitlines())
    return [truncate_line(line, width) for line in rows]


def compact_extension_label(workspace: Workspace) -> str:
    summary = extension_summary(workspace)
    return f"{summary['skills']} skills | {summary['mcp_enabled']}/{summary['mcp_total']} mcp | /extensions"


def tui_section(title: str, width: int) -> list[str]:
    label = f"[ {title} ]"
    return [truncate_line(label, width)]


def tui_kv(key: str, value: Any, width: int) -> str:
    return truncate_line(f"{key:<9} {value}", width)


def tui_flow_panel(last_report: dict[str, Any] | None, width: int) -> list[str]:
    rows = tui_section("Flow", width)
    if not last_report:
        rows.extend(
            [
                tui_kv("mode", "standing by", width),
                tui_kv("next", "plan, act, trace", width),
                tui_kv("proof", "/runs + /cost", width),
            ]
        )
        return rows
    steps = last_report.get("steps", [])
    actions = [str((step.get("action") or {}).get("action") or "none") for step in steps]
    rows.extend(
        [
            tui_kv("status", last_report.get("status", "unknown"), width),
            tui_kv("actions", " -> ".join(actions) if actions else "none", width),
            tui_token_meter(int(last_report.get("totals", {}).get("total_tokens", 0) or 0), width, report_budget_total(last_report)),
            tui_kv("proof", f"/cost run {str(last_report.get('id', ''))[:8]}", width),
        ]
    )
    return rows


def tui_task_panel(workspace: Workspace, task_id: str, width: int) -> list[str]:
    rows = tui_section("Task", width)
    try:
        task = TaskStore(workspace).get(task_id)
    except (KeyError, FileNotFoundError):
        rows.extend([tui_kv("status", "unknown", width)])
        return rows
    rows.extend(
        [
            tui_kv("status", task.get("status", "unknown"), width),
            tui_kv("title", task.get("title", ""), width),
        ]
    )
    plan = task.get("plan")
    if isinstance(plan, dict):
        progress = plan.get("milestones", [])
        completed = sum(1 for item in progress if item.get("status") == "completed")
        active = next((item for item in progress if item.get("status") == "active"), None)
        rows.append(tui_kv("plan", f"{completed}/{len(progress)} done", width))
        if active:
            rows.append(tui_kv("active", f"{active.get('id')} {active.get('title', '')}", width))
    rows.append("")
    return rows


def tui_last_run_panel(report: dict[str, Any], width: int) -> list[str]:
    rows = tui_section("Last Run", width)
    steps = report.get("steps", [])
    if steps:
        compact_actions = " -> ".join(str((step.get("action") or {}).get("action") or "none") for step in steps)
        rows.append(tui_kv("actions", compact_actions, width))
    rows.extend(
        [
            tui_kv("status", report.get("status"), width),
            tui_kv("tokens", report.get("totals", {}).get("total_tokens", 0), width),
        ]
    )
    rows.append(tui_token_meter(int(report.get("totals", {}).get("total_tokens", 0) or 0), width, report_budget_total(report)))
    if steps:
        rows.extend(tui_section("Steps", width))
        for step in steps[:4]:
            action = str((step.get("action") or {}).get("action") or "none")
            ok = "ok" if step.get("verifier_ok", True) else "check"
            rows.append(tui_timeline_row(step, action, ok, width))
        if len(steps) > 4:
            rows.append(f"  ... +{len(steps) - 4} more")
    return rows


def slice_tui_body(lines: list[str], height: int, scroll_offset: int, width: int) -> list[str]:
    if len(lines) <= height:
        return lines
    offset = max(0, min(scroll_offset, len(lines) - height))
    end = len(lines) - offset
    start = max(0, end - height)
    window = lines[start:end]
    if offset:
        window = [truncate_line(f"History view: {offset} line(s) above latest. Use /down or /latest.", width)] + window[1:]
    return window
