from __future__ import annotations

from pathlib import Path
from typing import Any

from .skills import SkillRegistry, validate_skill_file
from .storage import Workspace


def default_marketplace_index() -> Path:
    return Path(__file__).resolve().parent / "marketplace_data" / "skills" / "index.json"


def load_marketplace(index: Path | None = None) -> dict[str, Any]:
    path = index or default_marketplace_index()
    data = Workspace.read_json(path)
    entries = data.get("skills", [])
    if not isinstance(entries, list):
        raise ValueError(f"Marketplace index has invalid skills array: {path}")
    return {
        "path": str(path),
        "skills": entries,
    }


def list_marketplace_skills(index: Path | None = None) -> list[dict[str, Any]]:
    return list(load_marketplace(index)["skills"])


def install_marketplace_skill(
    workspace: Workspace,
    skill_id: str,
    *,
    index: Path | None = None,
) -> dict[str, Any]:
    market = load_marketplace(index)
    index_path = Path(market["path"])
    entries = {str(item.get("id")): item for item in market["skills"] if isinstance(item, dict)}
    entry = entries.get(skill_id)
    if not entry:
        raise KeyError(f"Unknown marketplace skill: {skill_id}")
    source = Path(str(entry.get("path", "")))
    if not source.is_absolute():
        source = (index_path.parent / source).resolve()
    validation = validate_skill_file(source)
    if not validation["ok"]:
        raise ValueError(f"Marketplace skill is invalid: {skill_id}")
    skill = SkillRegistry(workspace).register(source)
    return {
        "id": skill.id,
        "name": skill.name,
        "source": str(source),
        "workspace": str(workspace.root),
    }
