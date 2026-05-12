from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    summary: str
    intent: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)
    procedure: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Skill":
        required = ["id", "name", "summary", "intent"]
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ValueError(f"Skill is missing required fields: {', '.join(missing)}")
        return Skill(
            id=str(data["id"]),
            name=str(data["name"]),
            summary=str(data["summary"]),
            intent=str(data["intent"]),
            inputs=list(data.get("inputs", [])),
            outputs=list(data.get("outputs", [])),
            constraints=list(data.get("constraints", [])),
            failure_modes=list(data.get("failure_modes", [])),
            procedure=list(data.get("procedure", [])),
            examples=list(data.get("examples", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "intent": self.intent,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "constraints": self.constraints,
            "failure_modes": self.failure_modes,
            "procedure": self.procedure,
            "examples": self.examples,
        }

    def render_level(self, level: str) -> dict[str, Any]:
        if level == "l0":
            return {"id": self.id, "name": self.name, "summary": self.summary}
        if level == "l1":
            return {
                "id": self.id,
                "name": self.name,
                "summary": self.summary,
                "intent": self.intent,
                "inputs": self.inputs,
                "outputs": self.outputs,
            }
        if level == "l2":
            data = self.render_level("l1")
            data.update(
                {
                    "constraints": self.constraints,
                    "failure_modes": self.failure_modes,
                }
            )
            return data
        if level == "l3":
            return self.to_dict()
        raise ValueError(f"Unsupported skill level: {level}")


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: str
    text: str
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    archived_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MemoryRecord":
        created_at = str(data.get("created_at", utc_now()))
        return MemoryRecord(
            id=str(data["id"]),
            kind=str(data["kind"]),
            text=str(data["text"]),
            tags=list(data.get("tags", [])),
            created_at=created_at,
            updated_at=str(data.get("updated_at", created_at)),
            archived_at=data.get("archived_at"),
        )


@dataclass(frozen=True)
class Budget:
    profile: str
    total: int
    request: int
    runtime: int
    memory: int
    skills: int
    reserve: int


@dataclass(frozen=True)
class SelectedSkill:
    skill: Skill
    level: str
    score: int
    reason: str
    matched_terms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SelectedMemory:
    record: MemoryRecord
    score: int
    reason: str
    matched_terms: list[str] = field(default_factory=list)
