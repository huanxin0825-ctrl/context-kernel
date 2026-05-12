from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_COST_TOKEN_TOLERANCE = 10


def build_eval_cost_report(report: dict[str, Any]) -> dict[str, Any]:
    return build_cost_report(
        kind="eval",
        report=report,
        items=report.get("tasks", []),
        source_label=str(report.get("fixture", "")),
    )


def build_benchmark_cost_report(report: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for fixture in report.get("fixtures", []):
        fixture_name = Path(str(fixture.get("fixture", ""))).name
        for task in fixture.get("tasks", []):
            item = dict(task)
            item["fixture"] = fixture_name
            items.append(item)
    return build_cost_report(
        kind="benchmark",
        report=report,
        items=items,
        source_label=str(report.get("benchmark", "")),
    )


def build_cost_report(*, kind: str, report: dict[str, Any], items: list[dict[str, Any]], source_label: str) -> dict[str, Any]:
    summaries = [cost_item_snapshot(item) for item in items]
    hotspots = sorted(summaries, key=lambda item: item["kernel_tokens"], reverse=True)[:5]
    low_savings = sorted(summaries, key=lambda item: item["savings_percent"])[:5]
    summary = report.get("summary", {})
    return {
        "kind": kind,
        "id": report.get("id"),
        "name": report.get("name"),
        "source": source_label,
        "summary": {
            "item_count": len(summaries),
            "kernel_tokens": int(summary.get("total_kernel_tokens", 0) or 0),
            "baseline_tokens": int(summary.get("total_baseline_tokens", 0) or 0),
            "savings_tokens": int(summary.get("total_savings_tokens", 0) or 0),
            "savings_percent": float(summary.get("total_savings_percent", 0) or 0),
            "average_savings_percent": float(summary.get("average_savings_percent", 0) or 0),
            "execution_tokens": int(summary.get("total_execution_tokens", 0) or 0),
            "passed_checks": int(summary.get("passed_checks", 0) or 0),
            "total_checks": int(summary.get("total_checks", 0) or 0),
            "executed_items": int(summary.get("executed_tasks", 0) or 0),
            "blocked_items": int(summary.get("blocked_tasks", 0) or 0),
        },
        "hotspots": hotspots,
        "low_savings": low_savings,
        "items": summaries,
    }


def cost_item_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    kernel = item.get("kernel", {})
    baseline = item.get("baseline", {})
    savings = item.get("savings", {})
    execution = item.get("execution", {}) if isinstance(item.get("execution"), dict) else {}
    checks = item.get("checks", {})
    kernel_tokens = int(kernel.get("estimated_tokens", 0) or 0)
    baseline_tokens = int(baseline.get("estimated_tokens", 0) or 0)
    execution_tokens = int(execution.get("total_tokens", 0) or 0)
    savings_tokens = max(0, baseline_tokens - kernel_tokens)
    savings_percent = float(savings.get("percent", 0) or 0)
    snapshot = {
        "id": item.get("id"),
        "profile": item.get("profile"),
        "kernel_tokens": kernel_tokens,
        "baseline_tokens": baseline_tokens,
        "savings_tokens": savings_tokens,
        "savings_percent": savings_percent,
        "execution_tokens": execution_tokens,
        "checks": f"{int(checks.get('passed', 0) or 0)}/{int(checks.get('total', 0) or 0)}",
        "executed": bool(execution) and not bool(execution.get("blocked")),
        "blocked": bool(execution.get("blocked")),
    }
    if item.get("fixture"):
        snapshot["fixture"] = item.get("fixture")
    return snapshot


def render_cost_report(cost: dict[str, Any]) -> str:
    summary = cost["summary"]
    lines = [
        f"{cost['kind']}_cost: {cost['id']}",
        f"name: {cost.get('name', '')}",
        f"source: {cost.get('source', '')}",
        f"items: {summary['item_count']}",
        (
            f"tokens: kernel={summary['kernel_tokens']} baseline={summary['baseline_tokens']} "
            f"savings={summary['savings_tokens']} ({summary['savings_percent']}%) "
            f"execution={summary['execution_tokens']}"
        ),
        (
            f"checks: passed={summary['passed_checks']}/{summary['total_checks']} "
            f"executed={summary['executed_items']} blocked={summary['blocked_items']}"
        ),
        f"average_savings_percent: {summary['average_savings_percent']}",
    ]
    if cost["hotspots"]:
        hotspot = cost["hotspots"][0]
        scope = cost_scope(hotspot)
        lines.append(
            f"hotspot: {scope} kernel={hotspot['kernel_tokens']} baseline={hotspot['baseline_tokens']} "
            f"savings={hotspot['savings_percent']}% checks={hotspot['checks']}"
        )
    if cost["low_savings"]:
        weakest = cost["low_savings"][0]
        scope = cost_scope(weakest)
        lines.append(
            f"weakest_savings: {scope} kernel={weakest['kernel_tokens']} baseline={weakest['baseline_tokens']} "
            f"savings={weakest['savings_percent']}% checks={weakest['checks']}"
        )
    lines.append("")
    lines.append("Hotspots")
    for item in cost["hotspots"]:
        lines.append(cost_item_line(item))
    lines.append("")
    lines.append("Lowest Savings")
    for item in cost["low_savings"]:
        lines.append(cost_item_line(item))
    return "\n".join(lines).rstrip()


def render_cost_markdown(cost: dict[str, Any]) -> str:
    summary = cost["summary"]
    lines = [
        f"- Items: `{summary['item_count']}`",
        f"- Kernel tokens: `{summary['kernel_tokens']}`",
        f"- Baseline tokens: `{summary['baseline_tokens']}`",
        f"- Savings: `{summary['savings_tokens']}` tokens (`{summary['savings_percent']}%`)",
        f"- Execution tokens: `{summary['execution_tokens']}`",
        f"- Checks: `{summary['passed_checks']}/{summary['total_checks']}`",
        f"- Executed items: `{summary['executed_items']}`",
        f"- Blocked items: `{summary['blocked_items']}`",
        "",
        "### Hotspots",
        "",
        "| Scope | Kernel | Baseline | Savings | Checks | Execution |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in cost["hotspots"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    cost_scope(item),
                    str(item["kernel_tokens"]),
                    str(item["baseline_tokens"]),
                    f"{item['savings_percent']}%",
                    item["checks"],
                    str(item["execution_tokens"]),
                ]
            )
            + " |"
        )
    lines.extend(["", "### Lowest Savings", "", "| Scope | Kernel | Baseline | Savings | Checks | Execution |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for item in cost["low_savings"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    cost_scope(item),
                    str(item["kernel_tokens"]),
                    str(item["baseline_tokens"]),
                    f"{item['savings_percent']}%",
                    item["checks"],
                    str(item["execution_tokens"]),
                ]
            )
            + " |"
        )
    return "\n".join(lines).rstrip()


def diff_cost_reports(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    token_tolerance: int = DEFAULT_COST_TOKEN_TOLERANCE,
) -> dict[str, Any]:
    before_summary = before.get("summary", {})
    after_summary = after.get("summary", {})
    before_hotspot = first_item(before.get("hotspots", []))
    after_hotspot = first_item(after.get("hotspots", []))
    before_weakest = first_item(before.get("low_savings", []))
    after_weakest = first_item(after.get("low_savings", []))
    item_diffs = diff_cost_items(before.get("items", []), after.get("items", []))
    regressions: list[dict[str, Any]] = []

    execution_delta = int(scalar_delta(before_summary, after_summary, "execution_tokens"))
    if execution_delta > token_tolerance:
        regressions.append(
            {
                "kind": "execution_tokens",
                "message": f"execution tokens increased by {execution_delta}",
                "delta": execution_delta,
            }
        )

    hotspot_change = focus_change(before_hotspot, after_hotspot, metric_key="kernel_tokens")
    if hotspot_change["metric_delta"] > token_tolerance:
        regressions.append(
            {
                "kind": "hotspot_kernel_tokens",
                "message": (
                    f"hotspot kernel tokens increased from {hotspot_change['before_value']} "
                    f"to {hotspot_change['after_value']}"
                ),
                "scope": hotspot_change["after_scope"] or hotspot_change["before_scope"],
                "delta": hotspot_change["metric_delta"],
            }
        )

    weakest_savings_change = focus_change(before_weakest, after_weakest, metric_key="savings_percent")
    if weakest_savings_change["metric_delta"] < 0:
        regressions.append(
            {
                "kind": "weakest_savings_percent",
                "message": (
                    f"lowest savings dropped from {weakest_savings_change['before_value']}% "
                    f"to {weakest_savings_change['after_value']}%"
                ),
                "scope": weakest_savings_change["after_scope"] or weakest_savings_change["before_scope"],
                "delta": weakest_savings_change["metric_delta"],
            }
        )

    execution_item_regressions = [
        item
        for item in item_diffs
        if item["status"] == "changed" and item["execution_tokens_delta"] > token_tolerance
    ]
    for item in execution_item_regressions:
        regressions.append(
            {
                "kind": "item_execution_tokens",
                "message": f"{item['scope']} execution tokens increased by {item['execution_tokens_delta']}",
                "scope": item["scope"],
                "delta": item["execution_tokens_delta"],
            }
        )

    return {
        "before": cost_report_ref(before),
        "after": cost_report_ref(after),
        "summary_delta": {
            "items": int(scalar_delta(before_summary, after_summary, "item_count")),
            "kernel_tokens": int(scalar_delta(before_summary, after_summary, "kernel_tokens")),
            "baseline_tokens": int(scalar_delta(before_summary, after_summary, "baseline_tokens")),
            "savings_tokens": int(scalar_delta(before_summary, after_summary, "savings_tokens")),
            "savings_percent": round(scalar_delta(before_summary, after_summary, "savings_percent"), 2),
            "average_savings_percent": round(scalar_delta(before_summary, after_summary, "average_savings_percent"), 2),
            "execution_tokens": execution_delta,
            "passed_checks": int(scalar_delta(before_summary, after_summary, "passed_checks")),
            "total_checks": int(scalar_delta(before_summary, after_summary, "total_checks")),
        },
        "hotspot_change": hotspot_change,
        "weakest_savings_change": weakest_savings_change,
        "items": item_diffs,
        "regressions": regressions,
        "ok": not regressions,
    }


def diff_cost_items(before_items: list[dict[str, Any]], after_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before_by_scope = {cost_scope(item): item for item in before_items}
    after_by_scope = {cost_scope(item): item for item in after_items}
    scopes = sorted(set(before_by_scope).union(after_by_scope))
    diffs: list[dict[str, Any]] = []
    for scope in scopes:
        before = before_by_scope.get(scope)
        after = after_by_scope.get(scope)
        if before is None:
            diffs.append({"scope": scope, "status": "added"})
            continue
        if after is None:
            diffs.append({"scope": scope, "status": "removed"})
            continue
        diffs.append(
            {
                "scope": scope,
                "status": "changed",
                "kernel_tokens_delta": int(after.get("kernel_tokens", 0) or 0) - int(before.get("kernel_tokens", 0) or 0),
                "baseline_tokens_delta": int(after.get("baseline_tokens", 0) or 0) - int(before.get("baseline_tokens", 0) or 0),
                "savings_tokens_delta": int(after.get("savings_tokens", 0) or 0) - int(before.get("savings_tokens", 0) or 0),
                "savings_percent_delta": round(float(after.get("savings_percent", 0) or 0) - float(before.get("savings_percent", 0) or 0), 2),
                "execution_tokens_delta": int(after.get("execution_tokens", 0) or 0) - int(before.get("execution_tokens", 0) or 0),
                "before": compact_cost_item(before),
                "after": compact_cost_item(after),
            }
        )
    return diffs


def focus_change(before_item: dict[str, Any] | None, after_item: dict[str, Any] | None, *, metric_key: str) -> dict[str, Any]:
    before_value = metric_value(before_item, metric_key)
    after_value = metric_value(after_item, metric_key)
    return {
        "before_scope": cost_scope(before_item) if before_item else "",
        "after_scope": cost_scope(after_item) if after_item else "",
        "before_value": before_value,
        "after_value": after_value,
        "metric_key": metric_key,
        "metric_delta": round(after_value - before_value, 2),
    }


def compact_cost_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kernel_tokens": int(item.get("kernel_tokens", 0) or 0),
        "baseline_tokens": int(item.get("baseline_tokens", 0) or 0),
        "savings_percent": float(item.get("savings_percent", 0) or 0),
        "execution_tokens": int(item.get("execution_tokens", 0) or 0),
        "checks": item.get("checks"),
    }


def cost_report_ref(cost: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": cost.get("kind"),
        "id": cost.get("id"),
        "name": cost.get("name"),
        "source": cost.get("source"),
    }


def first_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return items[0] if items else None


def metric_value(item: dict[str, Any] | None, key: str) -> float:
    if not item:
        return 0.0
    try:
        return float(item.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def scalar_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> float:
    try:
        before_value = float(before.get(key, 0) or 0)
    except (TypeError, ValueError):
        before_value = 0.0
    try:
        after_value = float(after.get(key, 0) or 0)
    except (TypeError, ValueError):
        after_value = 0.0
    return after_value - before_value


def cost_scope(item: dict[str, Any]) -> str:
    fixture = item.get("fixture")
    task_id = str(item.get("id", ""))
    if fixture:
        return f"{fixture}/{task_id}"
    return task_id


def cost_item_line(item: dict[str, Any]) -> str:
    scope = cost_scope(item)
    return (
        f"- {scope}: kernel={item['kernel_tokens']} baseline={item['baseline_tokens']} "
        f"savings={item['savings_percent']}% checks={item['checks']} execution={item['execution_tokens']}"
    )
