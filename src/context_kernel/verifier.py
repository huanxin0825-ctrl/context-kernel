from __future__ import annotations

import json
from typing import Any


def verify_preflight(packet: dict[str, Any], *, allow_over_budget: bool = False) -> dict[str, Any]:
    budget = packet.get("budget", {})
    checks = [
        check("has_request", bool(packet.get("request")), "Context packet must include the user request."),
        check("has_budget", bool(budget), "Context packet must include a budget report."),
        check("has_runtime", bool(packet.get("runtime")), "Context packet must include runtime instructions."),
        check(
            "within_budget",
            allow_over_budget or not bool(budget.get("over_budget", True)),
            "Context packet is over budget; provider execution is blocked by default.",
        ),
    ]
    return summarize("preflight", checks)


def verify_response(response_text: str, *, expect_json: bool = False) -> dict[str, Any]:
    checks = [
        check("non_empty_response", bool(response_text.strip()), "Provider response must not be empty."),
    ]
    if expect_json:
        checks.append(
            check(
                "valid_json_response",
                parses_json(response_text),
                "Provider response must be valid JSON when --expect-json is set.",
            )
        )
    return summarize("response", checks)


def verify_trace(trace: dict[str, Any], *, expect_json: bool = False) -> dict[str, Any]:
    preflight = verify_preflight(trace.get("context_packet", {}), allow_over_budget=True)
    response = verify_response(trace.get("response", {}).get("text", ""), expect_json=expect_json)
    return combine_verifications("trace", preflight, response)


def enforce_preflight(packet: dict[str, Any], *, allow_over_budget: bool = False) -> dict[str, Any]:
    result = verify_preflight(packet, allow_over_budget=allow_over_budget)
    if not result["ok"]:
        failed = ", ".join(item["name"] for item in result["items"] if not item["passed"])
        raise RuntimeError(f"Preflight verification failed: {failed}")
    return result


def combine_verifications(stage: str, *results: dict[str, Any]) -> dict[str, Any]:
    checks = [item for result in results for item in result["items"]]
    return summarize(stage, checks)


def check(name: str, passed: bool, message: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "message": message}


def summarize(stage: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(1 for item in checks if item["passed"])
    return {
        "stage": stage,
        "ok": passed == len(checks),
        "passed": passed,
        "total": len(checks),
        "items": checks,
        "checks": {item["name"]: item["passed"] for item in checks},
    }


def parses_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True
