from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from .budget import DEFAULT_PROFILE
from .evals import diff_reports
from .evals import EvalRunner
from .models import utc_now
from .report_costs import build_benchmark_cost_report, diff_cost_reports, render_cost_markdown
from .storage import Workspace


class BenchmarkRunner:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.evals = EvalRunner(workspace)

    def run_directory(
        self,
        path: Path,
        default_budget: int | None = None,
        default_profile: str = DEFAULT_PROFILE,
        save: bool = True,
        execute_provider: str | None = None,
        execute_model: str | None = None,
        execute_base_url: str | None = None,
    ) -> dict[str, Any]:
        fixtures = sorted(path.glob("*.json"))
        if not fixtures:
            raise ValueError(f"Benchmark directory has no JSON fixtures: {path}")

        resolved_path = path.resolve()
        reports = [
            self.evals.run_fixture(
                fixture,
                default_budget=default_budget,
                default_profile=default_profile,
                save=False,
                execute_provider=execute_provider,
                execute_model=execute_model,
                execute_base_url=execute_base_url,
            )
            for fixture in fixtures
        ]
        report = {
            "id": uuid4().hex[:12],
            "created_at": utc_now(),
            "benchmark": str(path),
            "benchmark_path": str(resolved_path),
            "name": path.name,
            "execution": {
                "enabled": bool(execute_provider),
                "provider": execute_provider,
                "model": execute_model,
            },
            "fixtures": reports,
            "summary": summarize_benchmark(reports),
        }
        if save:
            self.save_report(report)
        return report

    def save_report(self, report: dict[str, Any]) -> Path:
        self.workspace.benchmarks_dir.mkdir(parents=True, exist_ok=True)
        path = self.workspace.benchmarks_dir / f"{report['id']}.json"
        Workspace.write_json(path, report)
        return path

    def list_reports(self) -> list[dict[str, Any]]:
        self.workspace.benchmarks_dir.mkdir(parents=True, exist_ok=True)
        reports: list[dict[str, Any]] = []
        for path in sorted(self.workspace.benchmarks_dir.glob("*.json")):
            report = Workspace.read_json(path)
            summary = report.get("summary", {})
            reports.append(
                {
                    "id": report.get("id", path.stem),
                    "created_at": report.get("created_at", ""),
                    "name": report.get("name", ""),
                    "fixture_count": summary.get("fixture_count", 0),
                    "task_count": summary.get("task_count", 0),
                    "average_savings_percent": summary.get("average_savings_percent", 0),
                    "checks": f"{summary.get('passed_checks', 0)}/{summary.get('total_checks', 0)}",
                    "ok": summary.get("ok", False),
                }
            )
        return sorted(reports, key=lambda item: item["created_at"], reverse=True)

    def get_report(self, report_id: str) -> dict[str, Any]:
        path = self.workspace.benchmarks_dir / f"{report_id}.json"
        if not path.exists():
            raise KeyError(f"Unknown benchmark report: {report_id}")
        return Workspace.read_json(path)

    def find_baseline(
        self,
        path: Path,
        *,
        baseline_id: str | None = None,
        exclude_id: str | None = None,
    ) -> dict[str, Any] | None:
        if baseline_id:
            return {"match": "explicit", "report": self.get_report(baseline_id)}

        self.workspace.benchmarks_dir.mkdir(parents=True, exist_ok=True)
        path_matches: list[dict[str, Any]] = []
        name_matches: list[dict[str, Any]] = []
        for report_path in sorted(self.workspace.benchmarks_dir.glob("*.json")):
            report = Workspace.read_json(report_path)
            if exclude_id and report.get("id") == exclude_id:
                continue
            if benchmark_path_matches(report, path):
                path_matches.append(report)
                continue
            if report.get("name") == path.name:
                name_matches.append(report)

        matches = path_matches or name_matches
        if not matches:
            return None
        matches.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return {
            "match": "path" if path_matches else "name",
            "report": matches[0],
        }

    def diff_reports(self, before_id: str, after_id: str) -> dict[str, Any]:
        before = self.get_report(before_id)
        after = self.get_report(after_id)
        return diff_benchmarks(before, after)

    def export_markdown(self, report_id: str, output: Path | None = None) -> Path:
        report = self.get_report(report_id)
        output = output or self.workspace.benchmarks_dir / f"{report_id}.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_benchmark_markdown(report), encoding="utf-8")
        return output

    def evidence(
        self,
        report_ids: list[str] | None = None,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        reports = [self.get_report(report_id) for report_id in report_ids] if report_ids else self._recent_reports(limit=limit)
        return build_benchmark_evidence(reports)

    def export_evidence_markdown(
        self,
        report_ids: list[str] | None = None,
        *,
        limit: int | None = None,
        output: Path | None = None,
    ) -> Path:
        evidence = self.evidence(report_ids, limit=limit)
        output = output or self.workspace.benchmarks_dir / "evidence.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_benchmark_evidence_markdown(evidence), encoding="utf-8")
        return output

    def _recent_reports(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        self.workspace.benchmarks_dir.mkdir(parents=True, exist_ok=True)
        reports = [Workspace.read_json(path) for path in sorted(self.workspace.benchmarks_dir.glob("*.json"))]
        reports.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        if limit is not None:
            reports = reports[: max(0, limit)]
        return reports


def summarize_benchmark(reports: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [report["summary"] for report in reports]
    total_tasks = sum(summary["task_count"] for summary in summaries)
    total_kernel = sum(summary["total_kernel_tokens"] for summary in summaries)
    total_baseline = sum(summary["total_baseline_tokens"] for summary in summaries)
    total_savings = max(0, total_baseline - total_kernel)
    total_checks = sum(summary["total_checks"] for summary in summaries)
    passed_checks = sum(summary["passed_checks"] for summary in summaries)
    total_execution_tokens = sum(summary.get("total_execution_tokens", 0) for summary in summaries)
    executed_tasks = sum(summary.get("executed_tasks", 0) for summary in summaries)
    blocked_tasks = sum(summary.get("blocked_tasks", 0) for summary in summaries)
    average_savings = (
        sum(summary["average_savings_percent"] for summary in summaries) / len(summaries)
        if summaries
        else 0.0
    )
    return {
        "fixture_count": len(reports),
        "task_count": total_tasks,
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


def build_benchmark_evidence(reports: list[dict[str, Any]]) -> dict[str, Any]:
    reports = sorted(reports, key=lambda item: item.get("created_at", ""), reverse=True)
    summaries = [report.get("summary", {}) for report in reports]
    total_kernel = sum(int(summary.get("total_kernel_tokens", 0) or 0) for summary in summaries)
    total_baseline = sum(int(summary.get("total_baseline_tokens", 0) or 0) for summary in summaries)
    total_savings = max(0, total_baseline - total_kernel)
    total_checks = sum(int(summary.get("total_checks", 0) or 0) for summary in summaries)
    passed_checks = sum(int(summary.get("passed_checks", 0) or 0) for summary in summaries)
    tasks = benchmark_task_snapshots(reports)
    weakest = sorted(tasks, key=lambda item: item["savings_percent"])[:5]
    strongest = sorted(tasks, key=lambda item: item["savings_tokens"], reverse=True)[:5]
    return {
        "id": uuid4().hex[:12],
        "created_at": utc_now(),
        "report_count": len(reports),
        "benchmark_count": len({report.get("name", "") for report in reports if report.get("name")}),
        "fixture_count": sum(int(summary.get("fixture_count", 0) or 0) for summary in summaries),
        "task_count": sum(int(summary.get("task_count", 0) or 0) for summary in summaries),
        "total_kernel_tokens": total_kernel,
        "total_baseline_tokens": total_baseline,
        "total_savings_tokens": total_savings,
        "total_savings_percent": round((total_savings / total_baseline) * 100, 2) if total_baseline else 0.0,
        "average_report_savings_percent": round(
            sum(float(summary.get("total_savings_percent", 0) or 0) for summary in summaries) / len(summaries),
            2,
        )
        if summaries
        else 0.0,
        "passed_checks": passed_checks,
        "total_checks": total_checks,
        "pass_rate_percent": round((passed_checks / total_checks) * 100, 2) if total_checks else 0.0,
        "reports": [benchmark_ref(report) | {"summary": report.get("summary", {})} for report in reports],
        "strongest_savings": strongest,
        "weakest_savings": weakest,
        "ok": bool(reports) and passed_checks == total_checks,
    }


def benchmark_task_snapshots(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for report in reports:
        for fixture in report.get("fixtures", []):
            fixture_name = Path(str(fixture.get("fixture", ""))).name
            for task in fixture.get("tasks", []):
                kernel_tokens = int(task.get("kernel", {}).get("estimated_tokens", 0) or 0)
                baseline_tokens = int(task.get("baseline", {}).get("estimated_tokens", 0) or 0)
                checks = task.get("checks", {})
                snapshots.append(
                    {
                        "report_id": report.get("id"),
                        "benchmark": report.get("name"),
                        "fixture": fixture_name,
                        "task": task.get("id"),
                        "profile": task.get("profile"),
                        "kernel_tokens": kernel_tokens,
                        "baseline_tokens": baseline_tokens,
                        "savings_tokens": max(0, baseline_tokens - kernel_tokens),
                        "savings_percent": float(task.get("savings", {}).get("percent", 0) or 0),
                        "checks": f"{int(checks.get('passed', 0) or 0)}/{int(checks.get('total', 0) or 0)}",
                    }
                )
    return snapshots


def diff_benchmarks(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_summary = before.get("summary", {})
    after_summary = after.get("summary", {})
    fixture_diffs = diff_fixtures(before.get("fixtures", []), after.get("fixtures", []))
    cost_diff = diff_cost_reports(build_benchmark_cost_report(before), build_benchmark_cost_report(after))
    regressions = [
        fixture
        for fixture in fixture_diffs
        if fixture.get("status") == "changed"
        and (
            fixture["summary_delta"]["kernel_tokens"] > 10
            or fixture["summary_delta"]["savings_percent"] < 0
            or fixture["summary_delta"]["passed_checks"] < 0
            or fixture["regressions"]
        )
    ]
    return {
        "before": benchmark_ref(before),
        "after": benchmark_ref(after),
        "summary_delta": {
            "fixtures": delta(before_summary, after_summary, "fixture_count"),
            "tasks": delta(before_summary, after_summary, "task_count"),
            "kernel_tokens": delta(before_summary, after_summary, "total_kernel_tokens"),
            "baseline_tokens": delta(before_summary, after_summary, "total_baseline_tokens"),
            "savings_tokens": delta(before_summary, after_summary, "total_savings_tokens"),
            "savings_percent": round(delta(before_summary, after_summary, "total_savings_percent"), 2),
            "passed_checks": delta(before_summary, after_summary, "passed_checks"),
            "total_checks": delta(before_summary, after_summary, "total_checks"),
            "execution_tokens": delta(before_summary, after_summary, "total_execution_tokens"),
        },
        "cost_diff": cost_diff,
        "cost_regressions": cost_diff["regressions"],
        "fixtures": fixture_diffs,
        "regressions": regressions,
        "ok": not regressions and cost_diff["ok"],
    }


def diff_fixtures(before_fixtures: list[dict[str, Any]], after_fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before_by_name = {Path(fixture["fixture"]).name: fixture for fixture in before_fixtures}
    after_by_name = {Path(fixture["fixture"]).name: fixture for fixture in after_fixtures}
    names = sorted(set(before_by_name).union(after_by_name))
    diffs: list[dict[str, Any]] = []
    for name in names:
        before = before_by_name.get(name)
        after = after_by_name.get(name)
        if before is None:
            diffs.append({"fixture": name, "status": "added"})
            continue
        if after is None:
            diffs.append({"fixture": name, "status": "removed"})
            continue
        fixture_diff = diff_reports(before, after)
        diffs.append(
            {
                "fixture": name,
                "status": "changed",
                "summary_delta": fixture_diff["summary_delta"],
                "regressions": fixture_diff["regressions"],
                "ok": fixture_diff["ok"],
            }
        )
    return diffs


def render_benchmark_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    cost = build_benchmark_cost_report(report)
    lines = [
        f"# Benchmark Report: {report['name']}",
        "",
        f"- Report id: `{report['id']}`",
        f"- Created at: `{report['created_at']}`",
        f"- Benchmark: `{report['benchmark']}`",
        f"- Fixtures: `{summary['fixture_count']}`",
        f"- Tasks: `{summary['task_count']}`",
        f"- Average savings: `{summary['average_savings_percent']}%`",
        f"- Total savings: `{summary['total_savings_tokens']}` tokens (`{summary['total_savings_percent']}%`)",
        f"- Checks: `{summary['passed_checks']}/{summary['total_checks']}`",
        f"- Executed tasks: `{summary['executed_tasks']}`",
        f"- Execution tokens: `{summary['total_execution_tokens']}`",
        "",
        "## Cost View",
        "",
        render_cost_markdown(cost),
        "",
        "## Fixtures",
        "",
        "| Fixture | Tasks | Avg Savings | Checks | Execution Tokens |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for fixture in report["fixtures"]:
        fixture_summary = fixture["summary"]
        lines.append(
            "| "
            + " | ".join(
                [
                    Path(fixture["fixture"]).name,
                    str(fixture_summary["task_count"]),
                    f"{fixture_summary['average_savings_percent']}%",
                    f"{fixture_summary['passed_checks']}/{fixture_summary['total_checks']}",
                    str(fixture_summary["total_execution_tokens"]),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Tasks", ""])
    for fixture in report["fixtures"]:
        lines.append(f"### {Path(fixture['fixture']).name}")
        lines.append("")
        lines.append("| Task | Profile | Kernel Tokens | Baseline Tokens | Savings | Checks |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
        for task in fixture["tasks"]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        task["id"],
                        task["profile"],
                        str(task["kernel"]["estimated_tokens"]),
                        str(task["baseline"]["estimated_tokens"]),
                        f"{task['savings']['percent']}%",
                        f"{task['checks']['passed']}/{task['checks']['total']}",
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_benchmark_evidence_markdown(evidence: dict[str, Any]) -> str:
    lines = [
        "# Benchmark Evidence",
        "",
        f"- Evidence id: `{evidence['id']}`",
        f"- Created at: `{evidence['created_at']}`",
        f"- Reports: `{evidence['report_count']}`",
        f"- Benchmarks: `{evidence['benchmark_count']}`",
        f"- Fixtures: `{evidence['fixture_count']}`",
        f"- Tasks: `{evidence['task_count']}`",
        f"- Kernel tokens: `{evidence['total_kernel_tokens']}`",
        f"- Baseline tokens: `{evidence['total_baseline_tokens']}`",
        f"- Token savings: `{evidence['total_savings_tokens']}` (`{evidence['total_savings_percent']}%`)",
        f"- Checks: `{evidence['passed_checks']}/{evidence['total_checks']}` (`{evidence['pass_rate_percent']}%`)",
        "",
        "## Reports",
        "",
        "| Report | Benchmark | Tasks | Savings | Checks |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for report in evidence.get("reports", []):
        summary = report.get("summary", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(report.get("id", "")),
                    str(report.get("name", "")),
                    str(summary.get("task_count", 0)),
                    f"{summary.get('total_savings_tokens', 0)} ({summary.get('total_savings_percent', 0)}%)",
                    f"{summary.get('passed_checks', 0)}/{summary.get('total_checks', 0)}",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Strongest Savings", "", "| Scope | Kernel | Baseline | Savings | Checks |", "| --- | ---: | ---: | ---: | ---: |"])
    for item in evidence.get("strongest_savings", []):
        lines.append(benchmark_evidence_task_line(item))
    lines.extend(["", "## Weakest Savings", "", "| Scope | Kernel | Baseline | Savings | Checks |", "| --- | ---: | ---: | ---: | ---: |"])
    for item in evidence.get("weakest_savings", []):
        lines.append(benchmark_evidence_task_line(item))
    return "\n".join(lines).rstrip() + "\n"


def benchmark_evidence_task_line(item: dict[str, Any]) -> str:
    scope = f"{item.get('benchmark', '')}/{item.get('fixture', '')}/{item.get('task', '')}"
    return (
        "| "
        + " | ".join(
            [
                scope,
                str(item.get("kernel_tokens", 0)),
                str(item.get("baseline_tokens", 0)),
                f"{item.get('savings_tokens', 0)} ({item.get('savings_percent', 0)}%)",
                str(item.get("checks", "")),
            ]
        )
        + " |"
    )


def benchmark_ref(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": report.get("id"),
        "created_at": report.get("created_at"),
        "benchmark": report.get("benchmark"),
        "benchmark_path": report.get("benchmark_path"),
        "name": report.get("name"),
    }


def delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int | float:
    return after.get(key, 0) - before.get(key, 0)


def benchmark_path_matches(report: dict[str, Any], path: Path) -> bool:
    return bool(benchmark_path_keys(path).intersection(report_benchmark_keys(report)))


def benchmark_path_keys(path: Path) -> set[str]:
    return {key for key in {str(path), str(path.resolve())} if key}


def report_benchmark_keys(report: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for value in (report.get("benchmark_path"), report.get("benchmark")):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        report_path = Path(text)
        keys.update({text, str(report_path), str(report_path.resolve())})
    return {key for key in keys if key}
