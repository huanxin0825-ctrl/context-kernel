from __future__ import annotations

import re
from typing import Any

from .memory import MemoryStore
from .storage import Workspace


MARKER_KINDS = {
    "decision": "decision",
    "decided": "decision",
    "fact": "fact",
    "preference": "preference",
    "project state": "project_state",
    "project_state": "project_state",
    "task state": "task_state",
    "task_state": "task_state",
}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s]+"),
]


class StateWriter:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)

    def propose_from_trace(self, trace: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = [self._task_state_candidate(trace)]
        if trace.get("verifier", {}).get("ok"):
            candidates.extend(marker_candidates(trace))
        return [candidate for candidate in candidates if candidate["text"]]

    def write_from_trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        candidates = self.propose_from_trace(trace)
        records = [
            self.memory.add(candidate["kind"], candidate["text"], candidate["tags"])
            for candidate in candidates
        ]
        return {
            "enabled": True,
            "candidate_count": len(candidates),
            "written_count": len(records),
            "records": [record.to_dict() for record in records],
        }

    def _task_state_candidate(self, trace: dict[str, Any]) -> dict[str, Any]:
        response = trace.get("response", {})
        verifier = trace.get("verifier", {})
        status = "ok" if verifier.get("ok") else "failed"
        text = (
            f"Trace {trace.get('id')}: request '{compact(trace.get('request', ''))}' "
            f"completed with provider {trace.get('provider')} status={status}; "
            f"tokens={response.get('total_tokens', 0)}."
        )
        return {
            "kind": "task_state",
            "text": redact(text),
            "tags": trace_tags(trace),
            "source": "trace_summary",
        }


def marker_candidates(trace: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    response_text = trace.get("response", {}).get("text", "")
    for line in response_text.splitlines():
        match = re.match(r"^\s*([A-Za-z _]+)\s*:\s*(.+?)\s*$", line)
        if not match:
            continue
        marker = match.group(1).strip().casefold()
        kind = MARKER_KINDS.get(marker)
        if not kind:
            continue
        candidates.append(
            {
                "kind": kind,
                "text": redact(compact(match.group(2))),
                "tags": trace_tags(trace) + ["marker:" + marker.replace(" ", "_")],
                "source": "response_marker",
            }
        )
    return candidates


def trace_tags(trace: dict[str, Any]) -> list[str]:
    tags = ["auto", "trace:" + str(trace.get("id", ""))]
    provider = trace.get("provider")
    if provider:
        tags.append("provider:" + str(provider))
    return tags


def compact(text: str, limit: int = 280) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + "[REDACTED]" if match.groups() else "[REDACTED]", redacted)
    return redacted
