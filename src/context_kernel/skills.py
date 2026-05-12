from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .models import SelectedSkill, Skill
from .providers import get_provider
from .storage import Workspace
from .text import matched_terms
from .tokenizer import estimate_tokens


class SkillRegistry:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def register(self, source: Path) -> Skill:
        data = Workspace.read_json(source)
        skill = Skill.from_dict(data)
        destination = self.workspace.skills_dir / f"{skill.id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return skill

    def all(self) -> list[Skill]:
        skills: list[Skill] = []
        for path in sorted(self.workspace.skills_dir.glob("*.json")):
            skills.append(Skill.from_dict(Workspace.read_json(path)))
        return skills

    def get(self, skill_id: str) -> Skill:
        path = self.workspace.skills_dir / f"{skill_id}.json"
        if not path.exists():
            raise KeyError(f"Unknown skill: {skill_id}")
        return Skill.from_dict(Workspace.read_json(path))

    def select(self, request: str, budget_tokens: int, limit: int = 3) -> list[SelectedSkill]:
        candidates: list[SelectedSkill] = []
        for skill in self.all():
            haystack = " ".join(
                [skill.id, skill.name, skill.summary, skill.intent, " ".join(skill.inputs), " ".join(skill.outputs)]
            )
            matches = matched_terms(request, haystack)
            score = len(matches)
            if score > 0:
                candidates.append(
                    SelectedSkill(
                        skill=skill,
                        level="l1",
                        score=score,
                        reason=f"matched terms: {', '.join(matches)}",
                        matched_terms=matches,
                    )
                )

        if not candidates:
            fallback = self.all()[:1]
            candidates = [
                SelectedSkill(
                    skill=skill,
                    level="l0",
                    score=0,
                    reason="fallback summary loaded because no skill matched",
                    matched_terms=[],
                )
                for skill in fallback
            ]

        selected = sorted(candidates, key=lambda item: item.score, reverse=True)[:limit]
        remaining = budget_tokens
        adjusted: list[SelectedSkill] = []
        for item in selected:
            level = item.level if item.score == 0 else "l2" if remaining > 350 else "l1"
            rendered_tokens = estimate_tokens(item.skill.render_level(level))
            if rendered_tokens > remaining and level == "l2":
                level = "l1"
                rendered_tokens = estimate_tokens(item.skill.render_level(level))
            if rendered_tokens <= remaining:
                adjusted.append(
                    SelectedSkill(
                        skill=item.skill,
                        level=level,
                        score=item.score,
                        reason=item.reason,
                        matched_terms=item.matched_terms,
                    )
                )
                remaining -= rendered_tokens
        return adjusted


SECTION_ALIASES = {
    "inputs": "inputs",
    "input": "inputs",
    "outputs": "outputs",
    "output": "outputs",
    "constraints": "constraints",
    "constraint": "constraints",
    "rules": "constraints",
    "failure modes": "failure_modes",
    "failure mode": "failure_modes",
    "failures": "failure_modes",
    "procedure": "procedure",
    "workflow": "procedure",
    "steps": "procedure",
    "process": "procedure",
    "examples": "examples",
    "example": "examples",
    "intent": "intent",
    "summary": "summary",
}


def compile_markdown_skill(path: Path, skill_id: str | None = None) -> Skill:
    text = path.read_text(encoding="utf-8")
    title = first_heading(text) or title_from_filename(path)
    sections = markdown_sections(text)
    body_intro = intro_text(text)

    name = title.strip()
    inferred_id = skill_id or slugify(name)
    summary = first_sentence(section_text(sections, "summary") or body_intro or name)
    intent = first_sentence(section_text(sections, "intent") or summary)

    data = {
        "id": inferred_id,
        "name": name,
        "summary": summary,
        "intent": intent,
        "inputs": section_items(sections, "inputs"),
        "outputs": section_items(sections, "outputs"),
        "constraints": section_items(sections, "constraints"),
        "failure_modes": section_items(sections, "failure_modes"),
        "procedure": section_items(sections, "procedure") or fallback_procedure(body_intro),
        "examples": section_items(sections, "examples"),
    }
    return Skill.from_dict(data)


def compile_markdown_skill_with_provider(
    path: Path,
    provider_name: str,
    model: str | None = None,
    base_url: str | None = None,
    skill_id: str | None = None,
) -> tuple[Skill, dict[str, Any]]:
    markdown = path.read_text(encoding="utf-8")
    provider = get_provider(provider_name, model=model, base_url=base_url)
    requested_id = skill_id or slugify(first_heading(markdown) or title_from_filename(path))
    packet = {
        "request": "Compile this Markdown skill into Context Kernel skill JSON.",
        "runtime": {
            "instructions": [
                "Return only valid JSON with no commentary.",
                "Preserve concrete constraints, failure modes, and procedures.",
                "Use concise strings. Do not invent capabilities absent from the Markdown.",
            ]
        },
        "schema": {
            "id": "string",
            "name": "string",
            "summary": "string",
            "intent": "string",
            "inputs": ["string"],
            "outputs": ["string"],
            "constraints": ["string"],
            "failure_modes": ["string"],
            "procedure": ["string"],
            "examples": ["string"],
        },
        "requested_id": requested_id,
        "source_markdown": markdown,
        "budget": {"estimated_used": estimate_tokens(markdown)},
    }
    response = provider.run(packet)
    data = extract_json_object(response.text)
    data["id"] = skill_id or data.get("id") or requested_id
    skill = Skill.from_dict(data)
    metadata = {
        "provider": provider.name,
        "model": getattr(provider, "model", model),
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "total_tokens": response.input_tokens + response.output_tokens,
    }
    return skill, metadata


def validate_skill_file(path: Path) -> dict[str, Any]:
    skill = Skill.from_dict(Workspace.read_json(path))
    levels = {level: estimate_tokens(skill.render_level(level)) for level in ["l0", "l1", "l2", "l3"]}
    warnings: list[str] = []
    if not skill.inputs:
        warnings.append("inputs is empty")
    if not skill.outputs:
        warnings.append("outputs is empty")
    if not skill.constraints:
        warnings.append("constraints is empty")
    if not skill.procedure:
        warnings.append("procedure is empty")
    return {
        "ok": True,
        "id": skill.id,
        "name": skill.name,
        "level_tokens": levels,
        "warnings": warnings,
    }


def inspect_skill(skill: Skill, budget: int) -> dict[str, Any]:
    levels: list[dict[str, Any]] = []
    selected = "l0"
    for level in ["l0", "l1", "l2", "l3"]:
        rendered = skill.render_level(level)
        tokens = estimate_tokens(rendered)
        fits = tokens <= budget
        if fits:
            selected = level
        levels.append({"level": level, "tokens": tokens, "fits": fits, "content": rendered})
    return {"id": skill.id, "budget": budget, "selected_level": selected, "levels": levels}


def markdown_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = parse_heading(line)
        if heading:
            current = SECTION_ALIASES.get(heading.lower())
            if current:
                sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return sections


def section_items(sections: dict[str, list[str]], name: str) -> list[str]:
    lines = sections.get(name, [])
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        bullet = re.sub(r"^[-*]\s+", "", stripped)
        bullet = re.sub(r"^\d+[.)]\s+", "", bullet)
        items.append(bullet.strip())
    return items


def section_text(sections: dict[str, list[str]], name: str) -> str:
    return " ".join(line.strip() for line in sections.get(name, []) if line.strip())


def intro_text(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if parse_heading(line):
            if lines:
                break
            continue
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return " ".join(lines)


def first_heading(text: str) -> str | None:
    for line in text.splitlines():
        heading = parse_heading(line)
        if heading:
            return heading
    return None


def parse_heading(line: str) -> str | None:
    match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
    return match.group(1).strip() if match else None


def first_sentence(text: str) -> str:
    compact = " ".join(text.split())
    if not compact:
        return ""
    match = re.match(r"^(.+?[.!?])\s", compact)
    return match.group(1) if match else compact[:180]


def fallback_procedure(text: str) -> list[str]:
    return [first_sentence(text)] if text else []


def title_from_filename(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").title()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.lower()).strip("_")
    return slug or "compiled_skill"


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    elif not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start : end + 1]
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("Provider did not return a JSON object.")
    return data
