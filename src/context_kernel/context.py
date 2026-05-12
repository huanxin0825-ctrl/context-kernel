from __future__ import annotations

from typing import Any

from .budget import allocate_budget
from .memory import MemoryStore
from .policy import summarize_command_policy
from .skills import SkillRegistry
from .storage import Workspace
from .tasks import TaskStore
from .tokenizer import estimate_tokens


class ContextBuilder:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillRegistry(workspace)

    def build(
        self,
        request: str,
        total_budget: int | None,
        profile: str = "balanced",
        task_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        budget = allocate_budget(request, total_budget, profile)
        config = self.workspace.load_config()
        runtime_instructions = config.get("runtime_instructions", [])
        task_brief = TaskStore(self.workspace).brief(task_id) if task_id and resume else None

        selected_memory = self.memory.search(request, limit=6, budget_tokens=budget.memory)
        selected_skills = self.skills.select(request, budget_tokens=budget.skills)

        packet = {
            "request": request,
            "runtime": {
                "instructions": runtime_instructions,
                "budget_policy": "Load the smallest useful context packet. Escalate skill levels only when needed.",
                "command_policy": summarize_command_policy(self.workspace),
            },
            "task": {
                "resume": bool(task_brief),
                "brief": task_brief,
            },
            "memory": [
                {
                    "record": item.record.to_dict(),
                    "score": item.score,
                    "reason": item.reason,
                    "matched_terms": item.matched_terms,
                    "estimated_tokens": estimate_tokens(item.record.to_dict()),
                }
                for item in selected_memory
            ],
            "skills": [
                {
                    "level": item.level,
                    "score": item.score,
                    "reason": item.reason,
                    "matched_terms": item.matched_terms,
                    "estimated_tokens": estimate_tokens(item.skill.render_level(item.level)),
                    "contract": item.skill.render_level(item.level),
                }
                for item in selected_skills
            ],
        }

        used = estimate_tokens(packet)
        packet["budget"] = {
            "profile": budget.profile,
            "total": budget.total,
            "allocated": {
                "request": budget.request,
                "runtime": budget.runtime,
                "memory": budget.memory,
                "skills": budget.skills,
                "reserve": budget.reserve,
            },
            "estimated_used": used,
            "estimated_remaining": max(0, budget.total - used),
            "over_budget": used > budget.total,
        }
        packet["omissions"] = self._omissions(packet)
        return packet

    def build_baseline(self, request: str) -> dict[str, Any]:
        config = self.workspace.load_config()
        packet = {
            "request": request,
            "runtime": {
                "instructions": config.get("runtime_instructions", []),
                "budget_policy": "Naive baseline loads all memory and full skill procedures.",
                "command_policy": summarize_command_policy(self.workspace),
            },
            "memory": [record.to_dict() for record in self.memory.all()],
            "skills": [skill.render_level("l3") for skill in self.skills.all()],
        }
        packet["budget"] = {
            "estimated_used": estimate_tokens(packet),
            "memory_count": len(packet["memory"]),
            "skill_count": len(packet["skills"]),
            "skill_level": "l3",
        }
        return packet

    def compare(
        self,
        request: str,
        total_budget: int | None,
        profile: str = "balanced",
        task_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        kernel = self.build(request, total_budget, profile, task_id=task_id, resume=resume)
        baseline = self.build_baseline(request)
        kernel_tokens = kernel["budget"]["estimated_used"]
        baseline_tokens = baseline["budget"]["estimated_used"]
        savings = max(0, baseline_tokens - kernel_tokens)
        savings_ratio = savings / baseline_tokens if baseline_tokens else 0.0
        return {
            "request": request,
            "budget": kernel["budget"]["total"],
            "profile": kernel["budget"]["profile"],
            "kernel": {
                "estimated_tokens": kernel_tokens,
                "selected_memory": len(kernel["memory"]),
                "selected_skills": len(kernel["skills"]),
                "over_budget": kernel["budget"]["over_budget"],
                "packet": kernel,
            },
            "baseline": {
                "estimated_tokens": baseline_tokens,
                "loaded_memory": baseline["budget"]["memory_count"],
                "loaded_skills": baseline["budget"]["skill_count"],
                "skill_level": baseline["budget"]["skill_level"],
                "packet": baseline,
            },
            "savings": {
                "estimated_tokens": savings,
                "ratio": round(savings_ratio, 4),
                "percent": round(savings_ratio * 100, 2),
            },
        }

    @staticmethod
    def _omissions(packet: dict[str, Any]) -> list[str]:
        omissions: list[str] = []
        if packet.get("task", {}).get("resume") and not packet.get("task", {}).get("brief"):
            omissions.append("Task resume was requested but no task brief was loaded.")
        if not packet["memory"]:
            omissions.append("No relevant memory matched the request.")
        if not packet["skills"]:
            omissions.append("No skill contract matched the request.")
        if packet["budget"]["over_budget"]:
            omissions.append("Context packet exceeded the requested budget estimate.")
        return omissions
