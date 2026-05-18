from __future__ import annotations

import json
from typing import Any

from .loop_actions import compact
from .loop_recovery import diagnose_agent_exception
from .providers import env_value
from .runner import AgentRunner
from .storage import Workspace


DEFAULT_PRIMARY_MODEL = "gpt-5.5"
DEFAULT_AUXILIARY_MODEL = "gpt-5.3-codex"
MODEL_ROUTING_MODES = {"auto", "primary", "auxiliary"}
AUX_REVIEW_MODES = {"auto", "off", "always"}


def select_model_role(
    *,
    model_routing: str,
    plan: dict[str, Any],
    step_index: int,
    prior_steps: list[dict[str, Any]],
    profile: str,
) -> tuple[str, str]:
    if model_routing in {"primary", "auxiliary"}:
        return model_routing, f"forced by --model-routing {model_routing}"

    route = plan.get("route", {})
    complexity = route.get("complexity", "low")
    warnings = plan.get("warnings", [])
    serious_warnings = [warning for warning in warnings if is_primary_required_warning(str(warning))]
    if profile == "deep":
        return "primary", "deep profile keeps reasoning on the primary model"
    if complexity == "high":
        return "primary", "high-complexity route requires the primary model"
    if serious_warnings:
        return "primary", "policy or budget warnings require the primary model"
    if prior_steps:
        return "primary", "synthesis after tool/context steps stays on the primary model"
    if step_index == 1 and complexity in {"low", "medium"}:
        return "auxiliary", f"{complexity}-complexity first-step planning is delegated to the auxiliary model"
    return "primary", "default fallback to the primary model"


def is_primary_required_warning(warning: str) -> bool:
    text = warning.casefold()
    return any(term in text for term in ["over budget", "policy", "blocked", "destructive", "unsafe", "do not execute"])


def resolve_role_model(provider_name: str, model: str | None, aux_model: str | None, role: str) -> str | None:
    if provider_name != "openai":
        return aux_model if role == "auxiliary" else model
    if role == "auxiliary":
        return aux_model or env_value("AKERNEL_OPENAI_AUX_MODEL") or DEFAULT_AUXILIARY_MODEL
    return model or env_value("AKERNEL_OPENAI_MODEL") or DEFAULT_PRIMARY_MODEL


def run_auxiliary_review(
    workspace: Workspace,
    *,
    request: str,
    provider_name: str,
    plan: dict[str, Any],
    selected_role: str,
    selected_model: str | None,
    aux_model: str | None,
    base_url: str | None,
    budget: int | None,
    profile: str,
    task_id: str,
    allow_over_budget: bool,
    aux_review: str,
    routing_reason: str,
) -> dict[str, Any]:
    resolved_aux = resolve_role_model(provider_name, selected_model, aux_model, "auxiliary")
    if aux_review == "off":
        return {"enabled": False, "reason": "disabled by --aux-review off"}
    if aux_review == "auto" and selected_role != "primary":
        return {"enabled": False, "reason": "auto review only runs before primary-model steps"}
    if not resolved_aux:
        return {"enabled": False, "reason": "no auxiliary model configured for this provider"}

    try:
        trace = AgentRunner(workspace).run(
            request,
            provider_name=provider_name,
            budget=budget,
            profile=profile,
            model=resolved_aux,
            base_url=base_url,
            allow_over_budget=allow_over_budget,
            expect_json=True,
            remember=False,
            task_id=task_id,
            resume=True,
            packet_overrides=build_aux_review_packet(
                plan,
                selected_role=selected_role,
                selected_model=selected_model,
                aux_model=resolved_aux,
                routing_reason=routing_reason,
            ),
        )
    except Exception as exc:
        diagnostic = diagnose_agent_exception(exc)
        return {
            "enabled": True,
            "trace_id": None,
            "model": resolved_aux,
            "ok": False,
            "risk": "medium",
            "recommendation": "use_primary",
            "notes": [diagnostic["message"]],
            "tokens": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "verifier_ok": False,
            "diagnostic": diagnostic,
        }
    parsed = parse_aux_review(trace.get("response", {}).get("text", ""))
    tokens = trace.get("response", {})
    return {
        "enabled": True,
        "trace_id": trace["id"],
        "model": resolved_aux,
        "ok": parsed["ok"],
        "risk": parsed["risk"],
        "recommendation": parsed["recommendation"],
        "notes": parsed["notes"],
        "tokens": {
            "input_tokens": tokens.get("input_tokens", 0),
            "output_tokens": tokens.get("output_tokens", 0),
            "total_tokens": tokens.get("total_tokens", 0),
        },
        "verifier_ok": bool(trace.get("verifier", {}).get("ok")),
    }


def build_aux_review_packet(
    plan: dict[str, Any],
    *,
    selected_role: str,
    selected_model: str | None,
    aux_model: str | None,
    routing_reason: str,
) -> dict[str, Any]:
    route = plan.get("route", {})
    return {
        "agent": {
            "mode": "aux_review_v1",
            "review": {
                "selected_role": selected_role,
                "selected_model": selected_model,
                "aux_model": aux_model,
                "routing_reason": routing_reason,
                "route_mode": route.get("mode"),
                "complexity": route.get("complexity"),
                "warnings": plan.get("warnings", []),
            },
            "response_contract": {
                "type": "json_object",
                "rules": [
                    "Return only valid JSON.",
                    "Schema: {\"ok\": boolean, \"risk\": \"low|medium|high\", \"recommendation\": \"continue|use_primary|reduce_context|stop\", \"notes\": [\"short note\"]}.",
                    "Be conservative about policy or budget risk, but do not invent missing facts.",
                ],
            },
        }
    }


def parse_aux_review(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    risk = str(data.get("risk", "medium")).strip().lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    recommendation = str(data.get("recommendation", "continue")).strip().lower()
    if recommendation not in {"continue", "use_primary", "reduce_context", "stop"}:
        recommendation = "continue"
    notes = data.get("notes", [])
    if not isinstance(notes, list):
        notes = [str(notes)]
    return {
        "ok": bool(data.get("ok", True)),
        "risk": risk,
        "recommendation": recommendation,
        "notes": [compact(str(item), limit=160) for item in notes[:5]],
    }
