from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from .context import ContextBuilder
from .models import utc_now
from .providers import get_provider
from .state_writer import StateWriter
from .storage import Workspace
from .verifier import combine_verifications, enforce_preflight, verify_response


class AgentRunner:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def run(
        self,
        request: str,
        provider_name: str,
        budget: int | None,
        profile: str = "balanced",
        model: str | None = None,
        base_url: str | None = None,
        allow_over_budget: bool = False,
        expect_json: bool = False,
        remember: bool = False,
        task_id: str | None = None,
        resume: bool = False,
        packet_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        packet = ContextBuilder(self.workspace).build(
            request,
            budget,
            profile,
            task_id=task_id,
            resume=resume,
        )
        if packet_overrides:
            packet = merge_dicts(packet, packet_overrides)
        preflight = enforce_preflight(packet, allow_over_budget=allow_over_budget)
        provider = get_provider(provider_name, model=model, base_url=base_url)
        response = provider.run(packet)
        response_verifier = verify_response(response.text, expect_json=expect_json)
        trace = {
            "id": uuid4().hex[:12],
            "created_at": utc_now(),
            "provider": provider.name,
            "model": getattr(provider, "model", None),
            "request": request,
            "task_id": task_id,
            "resume": resume,
            "context_packet": packet,
            "response": {
                "text": response.text,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "total_tokens": response.input_tokens + response.output_tokens,
            },
            "verifier": combine_verifications("run", preflight, response_verifier),
            "state": {"enabled": False, "candidate_count": 0, "written_count": 0, "records": []},
        }
        if remember:
            trace["state"] = StateWriter(self.workspace).write_from_trace(trace)
        Workspace.write_json(self.workspace.traces_dir / f"{trace['id']}.json", trace)
        return trace


def merge_dicts(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
