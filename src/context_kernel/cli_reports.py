from __future__ import annotations

from pathlib import Path
from typing import Any

from .cli_output import print_json
from .storage import Workspace


def model_routing_summary(report: dict[str, Any]) -> str:
    parts = []
    for step in report.get("steps", []):
        role = step.get("model_role") or "primary"
        model = step.get("model") or "default"
        reason = step.get("routing_reason") or ""
        label = f"step {step.get('index')}: {role} ({model})"
        parts.append(f"{label} - {reason}" if reason else label)
    if not parts:
        routing = report.get("model_routing", {})
        return f"{routing.get('mode', 'auto')}: no provider step was executed"
    return "; ".join(parts)


def aux_review_summary(report: dict[str, Any]) -> str:
    parts = []
    for step in report.get("steps", []):
        review = step.get("aux_review", {})
        if not isinstance(review, dict) or not review.get("enabled"):
            continue
        parts.append(
            f"step {step.get('index')}: {review.get('risk')} risk, "
            f"{review.get('recommendation')} via {review.get('model')} "
            f"({review.get('tokens', {}).get('total_tokens', 0)}t)"
        )
    return "; ".join(parts)


def print_agent_report(report: dict[str, Any]) -> None:
    print(f"agent_run: {report['id']}")
    print(f"task: {report['task_id']}")
    print(f"status: {report['status']}")
    print(f"steps: {len(report['steps'])}/{report['max_steps']}")
    print(f"tokens: total={report['totals']['total_tokens']} input={report['totals']['input_tokens']} output={report['totals']['output_tokens']}")
    routing = report.get("model_routing", {})
    if routing:
        print(
            "model_routing: "
            f"mode={routing.get('mode')} "
            f"primary={routing.get('primary_model')} "
            f"auxiliary={routing.get('auxiliary_model')} "
            f"review={routing.get('aux_review')}"
        )
    if report.get("state", {}).get("enabled"):
        print(f"state: wrote {report['state']['written_count']} memory record(s)")
    print_agent_diagnostic(report.get("diagnostic"))
    for step in report["steps"]:
        trace = step["trace_id"] or "none"
        tokens = step.get("tokens", {}).get("total_tokens", 0)
        action = (step.get("action") or {}).get("action", "none")
        model_part = f" model={step.get('model_role')}:{step.get('model') or 'default'}" if step.get("model_role") else ""
        review = step.get("aux_review", {})
        review_part = ""
        if isinstance(review, dict) and review.get("enabled"):
            review_part = f" review={review.get('risk')}:{review.get('recommendation')}"
        tool = step.get("tool", {})
        tool_part = f" tool={tool.get('name')}:{tool.get('id')}" if tool else ""
        print(f"- step {step['index']}: {step['status']} action={action} trace={trace} tokens={tokens}{model_part}{review_part}{tool_part}")
        print_agent_diagnostic(step.get("diagnostic"), prefix="  ")
    if report.get("final_response"):
        print("")
        print(report["final_response"])


def print_agent_diagnostic(diagnostic: Any, *, prefix: str = "") -> None:
    if not isinstance(diagnostic, dict) or not diagnostic:
        return
    category = diagnostic.get("category") or "unknown"
    message = diagnostic.get("message") or ""
    suggestion = diagnostic.get("suggestion") or ""
    print(f"{prefix}diagnostic: {category}")
    if message:
        print(f"{prefix}reason: {message}")
    if suggestion:
        print(f"{prefix}next: {suggestion}")


def print_policy_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print_json(result)
        return
    print(f"{result['status']}: {result['kind']} {result['operation']} {result['subject']}")
    for reason in result["reasons"]:
        print(f"- {reason}")


def print_tool_result(result: dict[str, Any]) -> None:
    status = "blocked" if result["blocked"] else "ok" if result["ok"] else "failed"
    print(f"{status}: {result['tool']} trace={result['id']}")
    if result.get("error"):
        print(f"error: {result['error']}")
    print(f"policy: {result['policy']['status']}")


def list_agent_reports(workspace: Workspace) -> list[dict[str, Any]]:
    workspace.agent_runs_dir.mkdir(parents=True, exist_ok=True)
    reports = [Workspace.read_json(path) for path in sorted(workspace.agent_runs_dir.glob("*.json"))]
    return sorted(reports, key=lambda report: report.get("created_at", ""), reverse=True)


def print_benchmark_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"report: {report['id']}")
    print(f"benchmark: {report['benchmark']}")
    print(f"fixtures: {summary['fixture_count']}")
    print(f"tasks: {summary['task_count']}")
    print(f"avg_savings: {summary['average_savings_percent']}%")
    print(f"total_kernel_tokens: {summary['total_kernel_tokens']}")
    print(f"total_baseline_tokens: {summary['total_baseline_tokens']}")
    if summary["executed_tasks"]:
        print(f"executed_tasks: {summary['executed_tasks']}")
        print(f"execution_tokens: {summary['total_execution_tokens']}")
    if summary.get("blocked_tasks"):
        print(f"blocked_tasks: {summary['blocked_tasks']}")
    print(f"checks: {summary['passed_checks']}/{summary['total_checks']}")
    for fixture in report["fixtures"]:
        fixture_summary = fixture["summary"]
        print(
            f"{Path(fixture['fixture']).name}\t"
            f"tasks={fixture_summary['task_count']}\t"
            f"avg_savings={fixture_summary['average_savings_percent']}%\t"
            f"checks={fixture_summary['passed_checks']}/{fixture_summary['total_checks']}"
        )


def print_benchmark_check_summary(report: dict[str, Any]) -> None:
    summary = report.get("summary", {})
    print(f"current_checks: {summary.get('passed_checks', 0)}/{summary.get('total_checks', 0)}")


def benchmark_report_ok(report: dict[str, Any]) -> bool:
    return bool(report.get("summary", {}).get("ok", False))


def enforce_benchmark_report_gate(report: dict[str, Any], *, label: str) -> None:
    if benchmark_report_ok(report):
        return
    summary = report.get("summary", {})
    passed = summary.get("passed_checks", 0)
    total = summary.get("total_checks", 0)
    raise RuntimeError(f"{label} current benchmark checks failed: {passed}/{total}")


def print_benchmark_diff(diff: dict[str, Any]) -> None:
    summary = diff["summary_delta"]
    print(f"before: {diff['before']['id']}")
    print(f"after: {diff['after']['id']}")
    print(f"fixtures_delta: {summary['fixtures']}")
    print(f"tasks_delta: {summary['tasks']}")
    print(f"kernel_tokens_delta: {summary['kernel_tokens']}")
    print(f"baseline_tokens_delta: {summary['baseline_tokens']}")
    print(f"savings_tokens_delta: {summary['savings_tokens']}")
    print(f"savings_percent_delta: {summary['savings_percent']}")
    print(f"checks_delta: {summary['passed_checks']}/{summary['total_checks']}")
    print(f"execution_tokens_delta: {summary['execution_tokens']}")
    print(f"regressions: {len(diff['regressions'])}")
    cost_diff = diff.get("cost_diff", {})
    if cost_diff:
        hotspot = cost_diff.get("hotspot_change", {})
        weakest = cost_diff.get("weakest_savings_change", {})
        print(f"cost_regressions: {len(diff.get('cost_regressions', []))}")
        print(
            f"hotspot_delta: {hotspot.get('before_scope', '')} -> {hotspot.get('after_scope', '')} "
            f"({hotspot.get('metric_delta', 0)})"
        )
        print(
            f"weakest_savings_delta: {weakest.get('before_scope', '')} -> {weakest.get('after_scope', '')} "
            f"({weakest.get('metric_delta', 0)})"
        )
    for fixture in diff["fixtures"]:
        if fixture["status"] != "changed":
            print(f"{fixture['fixture']}\t{fixture['status']}")
            continue
        fixture_summary = fixture["summary_delta"]
        print(
            f"{fixture['fixture']}\t"
            f"kernel_delta={fixture_summary['kernel_tokens']}\t"
            f"savings_delta={fixture_summary['savings_percent']}%\t"
            f"checks_delta={fixture_summary['passed_checks']}/{fixture_summary['total_checks']}\t"
            f"regressions={len(fixture['regressions'])}"
        )


def enforce_regression_gate(diff: dict[str, Any], *, enabled: bool, label: str) -> None:
    if not enabled or diff.get("ok", True):
        return
    reasons: list[str] = []
    regressions = diff.get("regressions", [])
    cost_regressions = diff.get("cost_regressions", [])
    if regressions:
        reasons.append(f"{len(regressions)} behavior regression(s)")
    if cost_regressions:
        reasons.append(f"{len(cost_regressions)} cost regression(s)")
    if not reasons:
        reasons.append("regressions detected")
    raise RuntimeError(f"{label} found regressions: {', '.join(reasons)}")
