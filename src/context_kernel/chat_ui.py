from __future__ import annotations

from typing import Any

from .terminal import ascii_meter, truncate_line


def format_tui_report(report: dict[str, Any]) -> str:
    final_response = str(report.get("final_response") or "").strip()
    if final_response:
        return final_response

    diagnostic = report.get("diagnostic")
    if isinstance(diagnostic, dict) and diagnostic:
        category = diagnostic.get("category") or "unknown"
        message = str(diagnostic.get("message") or "").strip()
        suggestion = diagnostic.get("suggestion") or "Use /runs for details."
        task_id = str(report.get("task_id") or "").strip()
        lines = [f"Run failed: {category}", f"Outcome: {report.get('status', 'failed')}"]
        if message:
            lines.append(f"Reason: {message}")
        lines.append(f"Next: {suggestion}")
        if task_id:
            lines.append(f"Resume: akernel task brief {task_id}")
        return "\n".join(lines)

    status = report.get("status", "finished")
    run_id = report.get("id", "")
    return f"Run {status}. Use /runs for details. run={run_id}"


def tui_token_meter(tokens: int, width: int, limit: int = 1200) -> str:
    limit = max(1, int(limit or 1200))
    cells = max(8, min(18, width - 12))
    filled = min(cells, max(0, int(round((tokens / limit) * cells)))) if limit else 0
    bar = "#" * filled + "." * (cells - filled)
    return truncate_line(f"meter    [{bar}] {tokens}/{limit}", width)


def tui_status_bar(status: str, width: int) -> str:
    cells = max(8, min(18, width // 6))
    if status == "running":
        filled = max(2, cells // 2)
    elif status in {"ready", "responded"}:
        filled = cells
    elif status in {"blocked", "failed", "needs_review"}:
        filled = max(1, cells // 3)
    else:
        filled = max(1, cells // 4)
    return "[" + "=" * filled + "." * (cells - filled) + "]"


def tui_flow_summary(last_report: dict[str, Any] | None) -> str:
    if not last_report:
        return "plan -> action -> trace"
    steps = last_report.get("steps", [])
    actions = [str((step.get("action") or {}).get("action") or "none") for step in steps]
    action_text = " -> ".join(actions[:4]) if actions else "no actions"
    if len(actions) > 4:
        action_text += f" +{len(actions) - 4}"
    return f"{last_report.get('status', 'run')} | {action_text}"


def tui_timeline_row(step: dict[str, Any], action: str, ok: str, width: int) -> str:
    trace = str(step.get("trace_id") or "none")[:8]
    tool = step.get("tool") or {}
    tool_part = f" tool:{str(tool.get('id', ''))[:8]}" if tool else ""
    return truncate_line(f"  {step.get('index', '?')}. {action:<11} {ok:<5} trace:{trace}{tool_part}", width)


def chat_help_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    return [
        (
            "Command Palette - Navigate",
            [
                ("/help", "show this command palette"),
                ("/status", "show workspace and runtime status"),
                ("/model", "show primary and auxiliary model roles"),
                ("/config", "show setup and environment guidance"),
                ("/task", "print the current task session JSON"),
            ],
        ),
        (
            "Command Palette - Context",
            [
                ("@query", "search workspace files; use @1, @2... to attach a listed match"),
                ("@path", "mention an exact file path inside a task to attach it automatically"),
                ("!command", "run a policy-checked command and attach its summary"),
                ("/paste", "enter a multi-line task; finish with /end"),
                ("/compact", "show the compact task brief used for resume context"),
                ("/commands", "list saved project and user slash commands"),
            ],
        ),
        (
            "Command Palette - Extend",
            [
                ("/extensions", "show MCP servers and registered skills"),
                ("/mcp", "show MCP server/tool availability"),
                ("/mcp refresh <name>", "refresh a configured MCP server"),
                ("/mcp call <server> <tool>", "call a discovered MCP tool"),
                ("/skills", "show registered skills"),
                ("/skills recommend <task>", "rank useful skills for a task"),
                ("/skills install <id>", "install a packaged marketplace skill"),
            ],
        ),
        (
            "Command Palette - Observe",
            [
                ("/runs", "list recent agent runs"),
                ("/cost", "print the last agent run cost report"),
                ("/up", "show older transcript lines in the TUI viewport"),
                ("/down", "move the TUI viewport back toward latest messages"),
                ("/latest", "jump the TUI viewport to the latest messages"),
                ("/clear", "clear and redraw the session header"),
                ("/exit", "leave the interactive session"),
            ],
        ),
    ]


def format_chat_help_text() -> str:
    lines: list[str] = []
    for title, rows in chat_help_groups():
        lines.append(f"[ {title} ]")
        lines.extend(f"{name:<22} {description}" for name, description in rows)
        lines.append("")
    return "\n".join(lines).strip()


def format_agent_timeline(report: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for step in report.get("steps", []):
        action = str((step.get("action") or {}).get("action") or "none")
        status = str(step.get("status") or "unknown")
        tokens = int(step.get("tokens", {}).get("total_tokens", 0) or 0)
        trace = str(step.get("trace_id") or "none")[:10]
        model = step.get("model_role") or "primary"
        rows.append(
            f"{step.get('index', '?')}. {action:<11} {status:<16} {tokens:>5}t "
            f"model={model} trace={trace}"
        )
        diagnostic = step.get("diagnostic")
        if isinstance(diagnostic, dict) and diagnostic:
            rows.append(f"   diagnostic={diagnostic.get('category', 'unknown')}: {diagnostic.get('message', '')}")
    return rows


def report_budget_total(report: dict[str, Any], fallback: int = 1200) -> int:
    totals: list[int] = []
    for step in report.get("steps", []):
        budget = step.get("plan", {}).get("budget", {}) if isinstance(step, dict) else {}
        total = budget.get("total") if isinstance(budget, dict) else None
        try:
            if total:
                totals.append(int(total))
        except (TypeError, ValueError):
            continue
    if totals:
        return max(totals)
    return fallback


def token_meter_text(tokens: int, report: dict[str, Any], width: int = 18) -> str:
    return ascii_meter(tokens, report_budget_total(report), width)
