from __future__ import annotations

from typing import Any
from uuid import uuid4

from .memory import MemoryStore
from .models import utc_now
from .storage import Workspace
from .tokenizer import estimate_tokens


TASK_STATUSES = {"active", "blocked", "completed"}


class TaskStore:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.workspace.tasks_dir.mkdir(parents=True, exist_ok=True)

    def start(self, title: str, goal: str | None = None) -> dict[str, Any]:
        now = utc_now()
        task = {
            "id": uuid4().hex[:12],
            "title": compact(title),
            "goal": compact(goal or title, limit=1000),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "steps": [
                {
                    "id": uuid4().hex[:8],
                    "created_at": now,
                    "kind": "start",
                    "note": compact(goal or title),
                    "refs": {},
                }
            ],
            "refs": {"run_traces": [], "tool_traces": [], "memories": []},
        }
        self.save(task)
        return task

    def get(self, task_id: str) -> dict[str, Any]:
        path = self.workspace.tasks_dir / f"{task_id}.json"
        if not path.exists():
            raise KeyError(f"Unknown task: {task_id}")
        return Workspace.read_json(path)

    def save(self, task: dict[str, Any]) -> None:
        task["updated_at"] = utc_now()
        Workspace.write_json(self.workspace.tasks_dir / f"{task['id']}.json", task)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status and status not in TASK_STATUSES:
            raise ValueError(f"Unsupported task status: {status}")
        tasks = [Workspace.read_json(path) for path in sorted(self.workspace.tasks_dir.glob("*.json"))]
        if status:
            tasks = [task for task in tasks if task.get("status") == status]
        return sorted(tasks, key=lambda task: task.get("updated_at", ""), reverse=True)

    def summary(self, task_id: str, max_steps: int = 5) -> dict[str, Any]:
        task = self.get(task_id)
        steps = task.get("steps", [])[-max_steps:]
        return {
            "id": task["id"],
            "title": task["title"],
            "goal": task["goal"],
            "status": task["status"],
            "latest_steps": [
                {
                    "kind": step.get("kind"),
                    "note": step.get("note"),
                    "created_at": step.get("created_at"),
                }
                for step in steps
            ],
            "refs": {
                "run_traces": len(task.get("refs", {}).get("run_traces", [])),
                "tool_traces": len(task.get("refs", {}).get("tool_traces", [])),
                "memories": len(task.get("refs", {}).get("memories", [])),
            },
        }

    def brief(self, task_id: str, max_steps: int = 6, max_refs: int = 5) -> dict[str, Any]:
        task = self.get(task_id)
        steps = task.get("steps", [])[-max_steps:]
        memories = self._memory_refs(task, max_refs)
        run_traces = self._run_trace_refs(task, max_refs)
        tool_traces = self._tool_trace_refs(task, max_refs)
        brief = {
            "task": {
                "id": task["id"],
                "title": task["title"],
                "goal": task["goal"],
                "status": task["status"],
                "created_at": task["created_at"],
                "updated_at": task["updated_at"],
            },
            "recent_steps": [
                {
                    "kind": step.get("kind"),
                    "note": step.get("note"),
                    "created_at": step.get("created_at"),
                }
                for step in steps
            ],
            "linked_memory": memories,
            "linked_run_traces": run_traces,
            "linked_tool_traces": tool_traces,
            "resume_instructions": [
                "Use this task brief as the active working state.",
                "Do not replay full chat history unless this brief is insufficient.",
                "Prefer attaching new run/tool traces back to this task.",
            ],
        }
        brief["estimated_tokens"] = estimate_tokens(brief)
        return brief

    def step(self, task_id: str, note: str, *, kind: str = "note", refs: dict[str, Any] | None = None) -> dict[str, Any]:
        task = self.get(task_id)
        ensure_not_completed(task)
        task["steps"].append(
            {
                "id": uuid4().hex[:8],
                "created_at": utc_now(),
                "kind": kind,
                "note": compact(note, limit=1000),
                "refs": refs or {},
            }
        )
        self.save(task)
        return task

    def attach(self, task_id: str, ref_kind: str, ref_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        ensure_not_completed(task)
        bucket = normalize_ref_kind(ref_kind)
        if ref_id not in task["refs"][bucket]:
            task["refs"][bucket].append(ref_id)
        task["steps"].append(
            {
                "id": uuid4().hex[:8],
                "created_at": utc_now(),
                "kind": "attach",
                "note": f"Attached {ref_label(bucket)}: {ref_id}",
                "refs": {bucket: [ref_id]},
            }
        )
        self.save(task)
        return task

    def set_status(self, task_id: str, status: str, note: str | None = None) -> dict[str, Any]:
        if status not in TASK_STATUSES:
            raise ValueError(f"Unsupported task status: {status}")
        task = self.get(task_id)
        task["status"] = status
        if status == "completed":
            task["completed_at"] = utc_now()
        if note:
            task["steps"].append(
                {
                    "id": uuid4().hex[:8],
                    "created_at": utc_now(),
                    "kind": status,
                    "note": compact(note, limit=1000),
                    "refs": {},
                }
            )
        self.save(task)
        return task

    def _memory_refs(self, task: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        memory = MemoryStore(self.workspace)
        records: list[dict[str, Any]] = []
        for record_id in task.get("refs", {}).get("memories", [])[-limit:]:
            try:
                record = memory.get(record_id, include_archived=True)
            except KeyError:
                records.append({"id": record_id, "missing": True})
                continue
            records.append(
                {
                    "id": record.id,
                    "kind": record.kind,
                    "text": compact(record.text, limit=500),
                    "tags": record.tags,
                    "archived": bool(record.archived_at),
                }
            )
        return records

    def _run_trace_refs(self, task: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        traces: list[dict[str, Any]] = []
        for trace_id in task.get("refs", {}).get("run_traces", [])[-limit:]:
            path = self.workspace.traces_dir / f"{trace_id}.json"
            if not path.exists():
                traces.append({"id": trace_id, "missing": True})
                continue
            trace = Workspace.read_json(path)
            traces.append(
                {
                    "id": trace.get("id", trace_id),
                    "created_at": trace.get("created_at"),
                    "provider": trace.get("provider"),
                    "model": trace.get("model"),
                    "request": compact(trace.get("request", ""), limit=300),
                    "response_text": compact(trace.get("response", {}).get("text", ""), limit=500),
                    "verifier_ok": trace.get("verifier", {}).get("ok"),
                    "tokens": trace.get("response", {}).get("total_tokens"),
                }
            )
        return traces

    def _tool_trace_refs(self, task: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        traces: list[dict[str, Any]] = []
        for trace_id in task.get("refs", {}).get("tool_traces", [])[-limit:]:
            path = self.workspace.tool_traces_dir / f"{trace_id}.json"
            if not path.exists():
                traces.append({"id": trace_id, "missing": True})
                continue
            trace = Workspace.read_json(path)
            traces.append(
                {
                    "id": trace.get("id", trace_id),
                    "created_at": trace.get("created_at"),
                    "tool": trace.get("tool"),
                    "ok": trace.get("ok"),
                    "blocked": trace.get("blocked"),
                    "subject": trace.get("policy", {}).get("subject"),
                    "output_summary": summarize_tool_output(trace),
                    "error": trace.get("error"),
                }
            )
        return traces


def normalize_ref_kind(ref_kind: str) -> str:
    normalized = ref_kind.strip().lower().replace("-", "_")
    aliases = {
        "run": "run_traces",
        "run_trace": "run_traces",
        "run_traces": "run_traces",
        "tool": "tool_traces",
        "tool_trace": "tool_traces",
        "tool_traces": "tool_traces",
        "memory": "memories",
        "memories": "memories",
    }
    if normalized not in aliases:
        raise ValueError("ref kind must be one of: run, tool, memory")
    return aliases[normalized]


def ref_label(bucket: str) -> str:
    return {
        "run_traces": "run_trace",
        "tool_traces": "tool_trace",
        "memories": "memory",
    }.get(bucket, bucket)


def ensure_not_completed(task: dict[str, Any]) -> None:
    if task.get("status") == "completed":
        raise ValueError(f"Task is completed and cannot be modified: {task['id']}")


def compact(text: str, limit: int = 300) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def summarize_tool_output(trace: dict[str, Any]) -> str:
    output = trace.get("output", {})
    tool = trace.get("tool")
    if trace.get("blocked"):
        subject = compact(str(trace.get("policy", {}).get("subject", "")), limit=200)
        error = compact(str(trace.get("error", "blocked by policy")), limit=240)
        return f"subject={subject}; error={error}" if subject else error
    if trace.get("error") and not output:
        return compact(str(trace.get("error", "")), limit=240)
    if tool == "read_file":
        content = compact(str(output.get("content", "")), limit=1000)
        suffix = " (truncated)" if output.get("truncated") else ""
        return content + suffix if content else "read completed"
    if tool == "run_command":
        command = compact(str(output.get("command", "")), limit=160)
        stdout = compact(str(output.get("stdout", "")), limit=500)
        stderr = compact(str(output.get("stderr", "")), limit=300)
        exit_code = output.get("exit_code")
        parts = [f"command={command}" if command else ""]
        if exit_code is not None:
            parts.append(f"exit_code={exit_code}")
        if stdout:
            parts.append(f"stdout={stdout}")
        if stderr:
            parts.append(f"stderr={stderr}")
        return "; ".join(part for part in parts if part) or "command completed"
    if tool == "patch_file":
        path = compact(str(output.get("path", "")), limit=200)
        delta = output.get("delta_chars")
        return f"path={path}; delta_chars={delta}" if path else "patch completed"
    if tool == "batch_patch":
        applied_count = output.get("applied_count")
        rolled_back = output.get("rolled_back")
        results = output.get("results", [])
        edit_count = len(results) if isinstance(results, list) else 0
        return f"applied_count={applied_count}; rolled_back={rolled_back}; edits={edit_count}"
    if tool == "write_file":
        path = compact(str(output.get("path", "")), limit=200)
        chars = output.get("written_chars")
        return f"path={path}; written_chars={chars}" if path else "write completed"
    if tool == "delete_file":
        path = compact(str(output.get("path", "")), limit=200)
        size = output.get("deleted_bytes")
        return f"path={path}; deleted_bytes={size}" if path else "delete completed"
    return compact(str(output), limit=500)
