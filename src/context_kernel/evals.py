from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from .budget import DEFAULT_PROFILE
from .context import ContextBuilder
from .models import utc_now
from .providers import get_provider
from .report_costs import build_eval_cost_report, diff_cost_reports
from .storage import Workspace
from .verifier import combine_verifications, verify_preflight, verify_response


REGRESSION_TOKEN_TOLERANCE = 10


class EvalRunner:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.context = ContextBuilder(workspace)

    def run_fixture(
        self,
        path: Path,
        default_budget: int | None = None,
        default_profile: str = DEFAULT_PROFILE,
        save: bool = True,
        execute_provider: str | None = None,
        execute_model: str | None = None,
        execute_base_url: str | None = None,
    ) -> dict[str, Any]:
        fixture = Workspace.read_json(path)
        tasks = fixture.get("tasks", [])
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("Eval fixture must contain a non-empty `tasks` list.")

        reports = [
            self._run_task(
                task,
                default_budget=default_budget,
                default_profile=default_profile,
                execute_provider=execute_provider,
                execute_model=execute_model,
                execute_base_url=execute_base_url,
            )
            for task in tasks
        ]
        report = {
            "id": uuid4().hex[:12],
            "created_at": utc_now(),
            "fixture": str(path),
            "name": fixture.get("name", path.stem),
            "execution": {
                "enabled": bool(execute_provider),
                "provider": execute_provider,
                "model": execute_model,
            },
            "tasks": reports,
            "summary": summarize_reports(reports),
        }
        if save:
            self.save_report(report)
        return report

    def save_report(self, report: dict[str, Any]) -> Path:
        self.workspace.evals_dir.mkdir(parents=True, exist_ok=True)
        path = self.workspace.evals_dir / f"{report['id']}.json"
        Workspace.write_json(path, report)
        return path

    def list_reports(self) -> list[dict[str, Any]]:
        self.workspace.evals_dir.mkdir(parents=True, exist_ok=True)
        reports: list[dict[str, Any]] = []
        for path in sorted(self.workspace.evals_dir.glob("*.json")):
            report = Workspace.read_json(path)
            summary = report.get("summary", {})
            reports.append(
                {
                    "id": report.get("id", path.stem),
                    "created_at": report.get("created_at", ""),
                    "name": report.get("name", ""),
                    "fixture": report.get("fixture", ""),
                    "task_count": summary.get("task_count", 0),
                    "average_savings_percent": summary.get("average_savings_percent", 0),
                    "checks": f"{summary.get('passed_checks', 0)}/{summary.get('total_checks', 0)}",
                    "ok": summary.get("ok", False),
                }
            )
        return sorted(reports, key=lambda item: item["created_at"], reverse=True)

    def get_report(self, report_id: str) -> dict[str, Any]:
        path = self.workspace.evals_dir / f"{report_id}.json"
        if not path.exists():
            raise KeyError(f"Unknown eval report: {report_id}")
        return Workspace.read_json(path)

    def diff_reports(self, before_id: str, after_id: str) -> dict[str, Any]:
        before = self.get_report(before_id)
        after = self.get_report(after_id)
        return diff_reports(before, after)

    def _run_task(
        self,
        task: dict[str, Any],
        default_budget: int | None,
        default_profile: str,
        execute_provider: str | None,
        execute_model: str | None,
        execute_base_url: str | None,
    ) -> dict[str, Any]:
        request = task.get("request")
        if not request:
            raise ValueError("Eval task is missing required field: request")

        comparison = self.context.compare(
            str(request),
            total_budget=task.get("budget", default_budget),
            profile=str(task.get("profile", default_profile)),
        )
        execution = execute_task(comparison, execute_provider, execute_model, execute_base_url)
        checks = evaluate_checks(comparison, task, execution)
        report = {
            "id": str(task.get("id", request)),
            "request": str(request),
            "profile": comparison["profile"],
            "budget": comparison["budget"],
            "kernel": {
                "estimated_tokens": comparison["kernel"]["estimated_tokens"],
                "selected_memory": comparison["kernel"]["selected_memory"],
                "selected_skills": comparison["kernel"]["selected_skills"],
            },
            "baseline": {
                "estimated_tokens": comparison["baseline"]["estimated_tokens"],
                "loaded_memory": comparison["baseline"]["loaded_memory"],
                "loaded_skills": comparison["baseline"]["loaded_skills"],
            },
            "savings": comparison["savings"],
            "checks": checks,
        }
        if execution:
            report["execution"] = execution
        return report


def execute_task(
    comparison: dict[str, Any],
    provider_name: str | None,
    model: str | None,
    base_url: str | None,
) -> dict[str, Any] | None:
    if not provider_name:
        return None
    packet = comparison["kernel"]["packet"]
    preflight = verify_preflight(packet)
    if not preflight["ok"]:
        return {
            "provider": provider_name,
            "model": model,
            "blocked": True,
            "block_reason": "preflight_failed",
            "response": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "verifier": preflight,
        }
    provider = get_provider(provider_name, model=model, base_url=base_url)
    response = provider.run(packet)
    response_verifier = verify_response(response.text)
    return {
        "provider": provider.name,
        "model": getattr(provider, "model", model),
        "blocked": False,
        "response": response.text,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "total_tokens": response.input_tokens + response.output_tokens,
        "verifier": combine_verifications("eval_execution", preflight, response_verifier),
    }


def evaluate_checks(
    comparison: dict[str, Any],
    task: dict[str, Any],
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    packet = comparison["kernel"]["packet"]
    selected_skill_ids = [item["contract"]["id"] for item in packet["skills"]]
    selected_memory_text = " ".join(item["record"]["text"].lower() for item in packet["memory"])

    checks: list[dict[str, Any]] = []
    for skill_id in task.get("expected_skills", []):
        checks.append(
            {
                "name": f"expected_skill:{skill_id}",
                "passed": skill_id in selected_skill_ids,
            }
        )
    for term in task.get("expected_memory_terms", []):
        checks.append(
            {
                "name": f"expected_memory_term:{term}",
                "passed": str(term).lower() in selected_memory_text,
            }
        )
    if execution:
        response_text = execution.get("response", "").lower()
        for term in task.get("expected_response_terms", []):
            checks.append(
                {
                    "name": f"expected_response_term:{term}",
                    "passed": str(term).lower() in response_text,
                }
            )
    minimum_savings = task.get("minimum_savings_percent")
    if minimum_savings is not None:
        checks.append(
            {
                "name": f"minimum_savings_percent:{minimum_savings}",
                "passed": comparison["savings"]["percent"] >= float(minimum_savings),
            }
        )

    passed = sum(1 for check in checks if check["passed"])
    return {
        "passed": passed,
        "total": len(checks),
        "ok": passed == len(checks),
        "items": checks,
    }


def summarize_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    total_kernel = sum(report["kernel"]["estimated_tokens"] for report in reports)
    total_baseline = sum(report["baseline"]["estimated_tokens"] for report in reports)
    total_savings = max(0, total_baseline - total_kernel)
    total_checks = sum(report["checks"]["total"] for report in reports)
    passed_checks = sum(report["checks"]["passed"] for report in reports)
    total_execution_tokens = sum(report.get("execution", {}).get("total_tokens", 0) for report in reports)
    executed_tasks = sum(1 for report in reports if report.get("execution") and not report["execution"].get("blocked"))
    blocked_tasks = sum(1 for report in reports if report.get("execution", {}).get("blocked"))
    average_savings = (
        sum(report["savings"]["percent"] for report in reports) / len(reports)
        if reports
        else 0.0
    )
    return {
        "task_count": len(reports),
        "total_kernel_tokens": total_kernel,
        "total_baseline_tokens": total_baseline,
        "total_savings_tokens": total_savings,
        "total_savings_percent": round((total_savings / total_baseline) * 100, 2) if total_baseline else 0.0,
        "average_savings_percent": round(average_savings, 2),
        "passed_checks": passed_checks,
        "total_checks": total_checks,
        "executed_tasks": executed_tasks,
        "blocked_tasks": blocked_tasks,
        "total_execution_tokens": total_execution_tokens,
        "ok": passed_checks == total_checks,
    }


def diff_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_summary = before.get("summary", {})
    after_summary = after.get("summary", {})
    task_diffs = diff_tasks(before.get("tasks", []), after.get("tasks", []))
    cost_diff = diff_cost_reports(build_eval_cost_report(before), build_eval_cost_report(after))
    regressions = [
        item
        for item in task_diffs
        if item["status"] == "changed"
        and (
            item["kernel_token_delta"] > REGRESSION_TOKEN_TOLERANCE
            or item["savings_percent_delta"] < 0
            or item["passed_check_delta"] < 0
        )
    ]
    return {
        "before": {
            "id": before.get("id"),
            "created_at": before.get("created_at"),
            "name": before.get("name"),
        },
        "after": {
            "id": after.get("id"),
            "created_at": after.get("created_at"),
            "name": after.get("name"),
        },
        "summary_delta": {
            "kernel_tokens": delta(before_summary, after_summary, "total_kernel_tokens"),
            "baseline_tokens": delta(before_summary, after_summary, "total_baseline_tokens"),
            "savings_tokens": delta(before_summary, after_summary, "total_savings_tokens"),
            "savings_percent": round(delta(before_summary, after_summary, "total_savings_percent"), 2),
            "passed_checks": delta(before_summary, after_summary, "passed_checks"),
            "total_checks": delta(before_summary, after_summary, "total_checks"),
        },
        "cost_diff": cost_diff,
        "cost_regressions": cost_diff["regressions"],
        "tasks": task_diffs,
        "regressions": regressions,
        "ok": not regressions and cost_diff["ok"],
    }


def diff_tasks(before_tasks: list[dict[str, Any]], after_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before_by_id = {task["id"]: task for task in before_tasks}
    after_by_id = {task["id"]: task for task in after_tasks}
    task_ids = sorted(set(before_by_id).union(after_by_id))
    diffs: list[dict[str, Any]] = []
    for task_id in task_ids:
        before = before_by_id.get(task_id)
        after = after_by_id.get(task_id)
        if before is None:
            diffs.append({"id": task_id, "status": "added"})
            continue
        if after is None:
            diffs.append({"id": task_id, "status": "removed"})
            continue
        diffs.append(
            {
                "id": task_id,
                "status": "changed",
                "kernel_token_delta": after["kernel"]["estimated_tokens"] - before["kernel"]["estimated_tokens"],
                "baseline_token_delta": after["baseline"]["estimated_tokens"] - before["baseline"]["estimated_tokens"],
                "savings_percent_delta": round(after["savings"]["percent"] - before["savings"]["percent"], 2),
                "passed_check_delta": after["checks"]["passed"] - before["checks"]["passed"],
                "total_check_delta": after["checks"]["total"] - before["checks"]["total"],
                "before": task_snapshot(before),
                "after": task_snapshot(after),
            }
        )
    return diffs


def task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "kernel_tokens": task["kernel"]["estimated_tokens"],
        "baseline_tokens": task["baseline"]["estimated_tokens"],
        "savings_percent": task["savings"]["percent"],
        "checks": f"{task['checks']['passed']}/{task['checks']['total']}",
    }


def delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int | float:
    return after.get(key, 0) - before.get(key, 0)
