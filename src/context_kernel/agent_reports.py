from __future__ import annotations

from typing import Any

from .storage import Workspace


def load_agent_report(workspace: Workspace, run_id: str) -> dict[str, Any]:
    path = workspace.agent_runs_dir / f"{run_id}.json"
    if not path.exists():
        raise KeyError(f"Unknown agent run: {run_id}")
    return Workspace.read_json(path)


def build_agent_cost_report(report: dict[str, Any]) -> dict[str, Any]:
    steps = report.get("steps", [])
    step_summaries: list[dict[str, Any]] = []
    action_breakdown: dict[str, dict[str, int]] = {}
    task_brief_series: list[int] = []
    planned_context_series: list[int] = []

    for step in steps:
        action = str((step.get("action") or {}).get("action") or "unknown")
        tokens = normalize_tokens(step.get("tokens", {}))
        task_brief_tokens = read_int(step.get("plan", {}).get("task", {}).get("estimated_tokens"))
        planned_context_tokens = read_int(step.get("plan", {}).get("budget", {}).get("estimated_used"))
        task_brief_series.append(task_brief_tokens)
        planned_context_series.append(planned_context_tokens)
        tool = step.get("tool", {}) if isinstance(step.get("tool"), dict) else {}
        step_summary = {
            "index": read_int(step.get("index")),
            "action": action,
            "status": str(step.get("status") or ""),
            "input_tokens": tokens["input_tokens"],
            "output_tokens": tokens["output_tokens"],
            "total_tokens": tokens["total_tokens"],
            "task_brief_tokens": task_brief_tokens,
            "planned_context_tokens": planned_context_tokens,
            "trace_id": step.get("trace_id"),
            "tool_trace_id": step.get("tool_trace_id"),
            "tool": tool.get("name"),
        }
        step_summaries.append(step_summary)

        bucket = action_breakdown.setdefault(
            action,
            {"count": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        bucket["count"] += 1
        bucket["input_tokens"] += tokens["input_tokens"]
        bucket["output_tokens"] += tokens["output_tokens"]
        bucket["total_tokens"] += tokens["total_tokens"]

    totals = normalize_tokens(report.get("totals", {}))
    hotspots = sorted(step_summaries, key=lambda item: item["total_tokens"], reverse=True)[:3]
    return {
        "run_id": report.get("id"),
        "task_id": report.get("task_id"),
        "status": report.get("status"),
        "request": str(report.get("request") or ""),
        "summary": {
            "step_count": len(step_summaries),
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "total_tokens": totals["total_tokens"],
            "average_tokens_per_step": round(totals["total_tokens"] / len(step_summaries), 2) if step_summaries else 0.0,
            "max_step_tokens": hotspots[0]["total_tokens"] if hotspots else 0,
            "max_step_index": hotspots[0]["index"] if hotspots else 0,
            "request_chars": len(str(report.get("request") or "")),
            "final_response_chars": len(str(report.get("final_response") or "")),
            "action_breakdown": action_breakdown,
            "task_brief": summarize_series(task_brief_series),
            "planned_context": summarize_series(planned_context_series),
        },
        "steps": step_summaries,
        "hotspots": hotspots,
        "storage": report.get("storage", {}),
    }


def render_agent_cost_report(cost: dict[str, Any]) -> str:
    summary = cost["summary"]
    lines = [
        f"agent_run: {cost['run_id']}",
        f"task: {cost.get('task_id')}",
        f"status: {cost.get('status')}",
        f"steps: {summary['step_count']}",
        (
            f"tokens: total={summary['total_tokens']} "
            f"input={summary['input_tokens']} output={summary['output_tokens']} "
            f"avg_per_step={summary['average_tokens_per_step']}"
        ),
        (
            f"task_brief_tokens: first={summary['task_brief']['first_tokens']} "
            f"last={summary['task_brief']['last_tokens']} "
            f"peak={summary['task_brief']['peak_tokens']} "
            f"growth={summary['task_brief']['growth_tokens']}"
        ),
        (
            f"planned_context_tokens: first={summary['planned_context']['first_tokens']} "
            f"last={summary['planned_context']['last_tokens']} "
            f"peak={summary['planned_context']['peak_tokens']} "
            f"growth={summary['planned_context']['growth_tokens']}"
        ),
        f"hotspot: step={summary['max_step_index']} tokens={summary['max_step_tokens']}",
    ]
    if summary["action_breakdown"]:
        parts = []
        for action, bucket in summary["action_breakdown"].items():
            parts.append(f"{action}={bucket['count']}x/{bucket['total_tokens']}t")
        lines.append("actions: " + ", ".join(parts))
    lines.append("")
    lines.append("Step Breakdown")
    for step in cost["steps"]:
        lines.append(
            (
                f"- step {step['index']}: action={step['action']} status={step['status']} "
                f"tokens={step['total_tokens']} input={step['input_tokens']} output={step['output_tokens']} "
                f"brief={step['task_brief_tokens']} context={step['planned_context_tokens']}"
            )
        )
    return "\n".join(lines)


def summarize_series(values: list[int]) -> dict[str, int]:
    if not values:
        return {
            "first_tokens": 0,
            "last_tokens": 0,
            "peak_tokens": 0,
            "growth_tokens": 0,
        }
    return {
        "first_tokens": values[0],
        "last_tokens": values[-1],
        "peak_tokens": max(values),
        "growth_tokens": values[-1] - values[0],
    }


def normalize_tokens(tokens: dict[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": read_int(tokens.get("input_tokens")),
        "output_tokens": read_int(tokens.get("output_tokens")),
        "total_tokens": read_int(tokens.get("total_tokens")),
    }


def read_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
