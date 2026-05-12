from __future__ import annotations

from typing import Any
from uuid import uuid4

from .memory import MemoryStore
from .models import utc_now
from .storage import Workspace
from .tokenizer import estimate_tokens


TASK_STATUSES = {"active", "blocked", "completed"}
MILESTONE_STATUSES = {"pending", "active", "completed", "blocked", "skipped"}


class TaskStore:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.workspace.tasks_dir.mkdir(parents=True, exist_ok=True)

    def start(self, title: str, goal: str | None = None, *, with_plan: bool = False) -> dict[str, Any]:
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
        if with_plan:
            task["plan"] = build_task_plan(task["title"], task["goal"])
            task["steps"].append(
                {
                    "id": uuid4().hex[:8],
                    "created_at": now,
                    "kind": "plan",
                    "note": "Created structured long-task plan.",
                    "refs": {},
                }
            )
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
            "plan": summarize_task_plan(task),
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
            "plan": brief_task_plan(task),
            "resume_instructions": [
                "Use this task brief as the active working state.",
                "Do not replay full chat history unless this brief is insufficient.",
                "Prefer attaching new run/tool traces back to this task.",
                "If a structured plan exists, continue from its active milestone and update checkpoints when progress changes.",
            ],
        }
        brief["estimated_tokens"] = estimate_tokens(brief)
        return brief

    def plan(self, task_id: str, *, goal: str | None = None, force: bool = False) -> dict[str, Any]:
        task = self.get(task_id)
        ensure_not_completed(task)
        if task.get("plan") and not force:
            return task
        if goal:
            task["goal"] = compact(goal, limit=1000)
        task["plan"] = build_task_plan(task["title"], task["goal"])
        task["steps"].append(
            {
                "id": uuid4().hex[:8],
                "created_at": utc_now(),
                "kind": "plan",
                "note": "Created structured long-task plan.",
                "refs": {},
            }
        )
        self.save(task)
        return task

    def next_checkpoint(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        plan = task.get("plan") or build_task_plan(task["title"], task["goal"])
        milestone = active_milestone(plan)
        return {
            "task_id": task["id"],
            "task_status": task["status"],
            "milestone": milestone,
            "resume_prompt": checkpoint_resume_prompt(task, milestone),
            "plan_progress": plan_progress(plan),
        }

    def checkpoint(
        self,
        task_id: str,
        note: str,
        *,
        milestone_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        task = self.get(task_id)
        ensure_not_completed(task)
        if status and status not in MILESTONE_STATUSES:
            raise ValueError(f"Unsupported milestone status: {status}")
        plan = task.get("plan")
        selected: dict[str, Any] | None = None
        if plan:
            selected = find_milestone(plan, milestone_id) if milestone_id else active_milestone(plan)
            if selected and status:
                selected["status"] = status
                selected["updated_at"] = utc_now()
            if selected and status == "completed":
                activate_next_milestone(plan, selected["id"])
        refs = {"milestone": selected["id"]} if selected else {}
        task["steps"].append(
            {
                "id": uuid4().hex[:8],
                "created_at": utc_now(),
                "kind": "checkpoint",
                "note": compact(note, limit=1000),
                "refs": refs,
            }
        )
        if plan:
            plan["updated_at"] = utc_now()
        self.save(task)
        return task

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


def build_task_plan(title: str, goal: str, *, max_milestones: int = 5) -> dict[str, Any]:
    now = utc_now()
    objective = compact(goal or title, limit=1000)
    phases = [
        (
            "M1",
            "Investigate scope",
            "Collect first-hand evidence, constraints, existing behavior, and non-goals before editing.",
            ["Relevant files, commands, and risks are identified.", "No large context dump is required to resume."],
        ),
        (
            "M2",
            "Design bounded change",
            "Choose the smallest safe implementation path, split risky work, and define verification commands.",
            ["Implementation scope is explicit.", "Rollback or recovery path is known before edits."],
        ),
        (
            "M3",
            "Implement slice",
            "Make the next cohesive code or documentation change while preserving unrelated work.",
            ["Changed files match the planned slice.", "Task checkpoint records what changed and why."],
        ),
        (
            "M4",
            "Verify behavior",
            "Run targeted tests first, then broader checks appropriate for the project profile.",
            ["Verification commands and results are attached or summarized.", "New failures are classified before continuing."],
        ),
        (
            "M5",
            "Document and handoff",
            "Update user-facing notes, summarize residual risk, and prepare commit or follow-up work.",
            ["Docs or changelog reflect the behavior.", "Next action is clear without replaying chat history."],
        ),
    ][:max_milestones]
    milestones = [
        {
            "id": milestone_id,
            "title": title,
            "objective": objective_text,
            "status": "active" if index == 0 else "pending",
            "acceptance": acceptance,
            "updated_at": now,
        }
        for index, (milestone_id, title, objective_text, acceptance) in enumerate(phases)
    ]
    return {
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "objective": objective,
        "milestones": milestones,
        "completion_policy": [
            "All non-skipped milestones are completed or explicitly blocked with a reason.",
            "Latest verification evidence is linked through task refs or checkpoint notes.",
            "Final response reports completed work, tests, and residual risks.",
        ],
    }


def summarize_task_plan(task: dict[str, Any]) -> dict[str, Any] | None:
    plan = task.get("plan")
    if not plan:
        return None
    return {
        "progress": plan_progress(plan),
        "active": active_milestone(plan),
    }


def brief_task_plan(task: dict[str, Any]) -> dict[str, Any] | None:
    plan = task.get("plan")
    if not plan:
        return None
    milestones = plan.get("milestones", [])
    active = active_milestone(plan)
    return {
        "objective": compact(plan.get("objective", ""), limit=500),
        "progress": plan_progress(plan),
        "active_milestone": active,
        "milestones": [
            {
                "id": milestone.get("id"),
                "title": milestone.get("title"),
                "status": milestone.get("status"),
            }
            for milestone in milestones
        ],
        "completion_policy": plan.get("completion_policy", []),
    }


def plan_progress(plan: dict[str, Any]) -> dict[str, int]:
    milestones = plan.get("milestones", [])
    total = len(milestones)
    completed = sum(1 for item in milestones if item.get("status") == "completed")
    blocked = sum(1 for item in milestones if item.get("status") == "blocked")
    skipped = sum(1 for item in milestones if item.get("status") == "skipped")
    return {"total": total, "completed": completed, "blocked": blocked, "skipped": skipped}


def active_milestone(plan: dict[str, Any]) -> dict[str, Any] | None:
    milestones = plan.get("milestones", [])
    for milestone in milestones:
        if milestone.get("status") == "active":
            return milestone
    for milestone in milestones:
        if milestone.get("status") == "pending":
            milestone["status"] = "active"
            milestone["updated_at"] = utc_now()
            return milestone
    return milestones[-1] if milestones else None


def find_milestone(plan: dict[str, Any], milestone_id: str | None) -> dict[str, Any] | None:
    if not milestone_id:
        return None
    normalized = milestone_id.strip().casefold()
    for milestone in plan.get("milestones", []):
        if str(milestone.get("id", "")).casefold() == normalized:
            return milestone
    raise KeyError(f"Unknown milestone: {milestone_id}")


def activate_next_milestone(plan: dict[str, Any], completed_id: str) -> None:
    milestones = plan.get("milestones", [])
    for index, milestone in enumerate(milestones):
        if milestone.get("id") != completed_id:
            continue
        for next_milestone in milestones[index + 1 :]:
            if next_milestone.get("status") == "pending":
                next_milestone["status"] = "active"
                next_milestone["updated_at"] = utc_now()
                return
        return


def checkpoint_resume_prompt(task: dict[str, Any], milestone: dict[str, Any] | None) -> str:
    if not milestone:
        return f"Continue task {task['id']}: {task['goal']}"
    acceptance = "; ".join(milestone.get("acceptance", [])[:2])
    return (
        f"Continue task {task['id']} from {milestone.get('id')} {milestone.get('title')}: "
        f"{milestone.get('objective')} Acceptance: {acceptance}"
    )


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
