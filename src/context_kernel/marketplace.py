from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import url2pathname, urlopen

from . import __version__
from .models import Skill
from .storage import Workspace


SUPPORTED_INDEX_VERSION = 2


def default_marketplace_index() -> Path:
    return Path(__file__).resolve().parent / "marketplace_data" / "skills" / "index.json"


def load_marketplace(index: str | Path | None = None, *, allow_remote: bool = True) -> dict[str, Any]:
    reference = str(index) if index is not None else str(default_marketplace_index())
    data = load_json_reference(reference, allow_remote=allow_remote)
    entries = data.get("skills", [])
    if not isinstance(entries, list):
        raise ValueError(f"Marketplace index has invalid skills array: {reference}")
    index_version = int(data.get("version", 1))
    if index_version > SUPPORTED_INDEX_VERSION:
        raise ValueError(f"Marketplace index version {index_version} is newer than supported version {SUPPORTED_INDEX_VERSION}.")
    return {
        "path": reference,
        "version": index_version,
        "name": data.get("name", "Unnamed marketplace"),
        "trusted": not is_remote_reference(reference),
        "skills": [normalize_marketplace_entry(entry, reference) for entry in entries if isinstance(entry, dict)],
    }


def list_marketplace_skills(index: str | Path | None = None) -> list[dict[str, Any]]:
    return list(load_marketplace(index)["skills"])


def install_marketplace_skill(
    workspace: Workspace,
    skill_id: str,
    *,
    index: str | Path | None = None,
    trust_remote: bool = False,
    ignore_compat: bool = False,
) -> dict[str, Any]:
    market = load_marketplace(index)
    entries = {str(item.get("id")): item for item in market["skills"] if isinstance(item, dict)}
    entry = entries.get(skill_id)
    if not entry:
        raise KeyError(f"Unknown marketplace skill: {skill_id}")
    compatibility = check_entry_compatibility(entry)
    if not compatibility["ok"] and not ignore_compat:
        raise ValueError(f"Marketplace skill is not compatible: {skill_id}: {'; '.join(compatibility['warnings'])}")
    source_ref = str(entry.get("resolved_path") or entry.get("path") or "")
    if is_remote_reference(source_ref) and not trust_remote:
        raise PermissionError(f"Installing remote marketplace skill requires --trust-remote: {source_ref}")
    skill_data, source_label = load_skill_source(source_ref)
    validation = validate_skill_data(skill_data)
    if not validation["ok"]:
        raise ValueError(f"Marketplace skill is invalid: {skill_id}")
    skill = Skill.from_dict(skill_data)
    destination = workspace.skills_dir / f"{skill.id}.json"
    Workspace.write_json(destination, skill.to_dict())
    return {
        "id": skill.id,
        "name": skill.name,
        "version": entry.get("version", "0.0.0"),
        "source": source_label,
        "marketplace": market["name"],
        "compatibility": compatibility,
        "workspace": str(workspace.root),
    }


def normalize_marketplace_entry(entry: dict[str, Any], index_ref: str) -> dict[str, Any]:
    normalized = dict(entry)
    normalized.setdefault("version", "0.0.0")
    normalized.setdefault("license", "unknown")
    normalized.setdefault("trust", "packaged" if not is_remote_reference(index_ref) else "remote")
    path = str(normalized.get("path", ""))
    normalized["resolved_path"] = resolve_marketplace_path(index_ref, path)
    normalized["remote"] = is_remote_reference(str(normalized["resolved_path"]))
    normalized["compatibility"] = normalize_compatibility(normalized.get("compatibility"))
    normalized["compatibility_check"] = check_entry_compatibility(normalized)
    return normalized


def normalize_compatibility(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        return {"context_kernel": ">=0.1.0"}
    value = data.get("context_kernel") or data.get("context-kernel") or ">=0.1.0"
    return {"context_kernel": str(value)}


def check_entry_compatibility(entry: dict[str, Any]) -> dict[str, Any]:
    requirement = normalize_compatibility(entry.get("compatibility")).get("context_kernel", ">=0.1.0")
    ok = version_satisfies(__version__, requirement)
    warnings = [] if ok else [f"context_kernel {__version__} does not satisfy {requirement}"]
    return {"ok": ok, "context_kernel": requirement, "current": __version__, "warnings": warnings}


def version_satisfies(current: str, requirement: str) -> bool:
    requirement = requirement.strip()
    if not requirement or requirement == "*":
        return True
    for operator in [">=", "<=", "==", ">", "<"]:
        if requirement.startswith(operator):
            expected = requirement[len(operator) :].strip()
            comparison = compare_versions(current, expected)
            return {
                ">=": comparison >= 0,
                "<=": comparison <= 0,
                "==": comparison == 0,
                ">": comparison > 0,
                "<": comparison < 0,
            }[operator]
    return compare_versions(current, requirement) == 0


def compare_versions(left: str, right: str) -> int:
    left_parts = parse_version(left)
    right_parts = parse_version(right)
    max_len = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (max_len - len(left_parts)))
    right_parts.extend([0] * (max_len - len(right_parts)))
    return (left_parts > right_parts) - (left_parts < right_parts)


def parse_version(version: str) -> list[int]:
    parts: list[int] = []
    for part in version.replace("-", ".").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits or 0))
    return parts or [0]


def resolve_marketplace_path(index_ref: str, path: str) -> str:
    if not path:
        return path
    if is_remote_reference(path) or is_file_url(path):
        return path
    if is_remote_reference(index_ref):
        return urljoin(index_ref, path)
    base = Path(file_url_to_path(index_ref) if is_file_url(index_ref) else index_ref)
    return str((base.parent / path).resolve())


def load_skill_source(reference: str) -> tuple[dict[str, Any], str]:
    data = load_json_reference(reference, allow_remote=True)
    return data, reference


def validate_skill_data(data: dict[str, Any]) -> dict[str, Any]:
    skill = Skill.from_dict(data)
    return {
        "ok": True,
        "id": skill.id,
        "name": skill.name,
        "warnings": [],
    }


def load_json_reference(reference: str, *, allow_remote: bool) -> dict[str, Any]:
    if is_remote_reference(reference):
        if not allow_remote:
            raise PermissionError(f"Remote marketplace references are not allowed: {reference}")
        with urlopen(reference, timeout=15) as response:
            return json.loads(response.read().decode("utf-8-sig"))
    path = Path(file_url_to_path(reference) if is_file_url(reference) else reference)
    return Workspace.read_json(path)


def is_remote_reference(reference: str) -> bool:
    parsed = urlparse(str(reference))
    return parsed.scheme in {"http", "https"}


def is_file_url(reference: str) -> bool:
    return urlparse(str(reference)).scheme == "file"


def file_url_to_path(reference: str) -> str:
    parsed = urlparse(reference)
    path = url2pathname(parsed.path)
    if parsed.netloc:
        return f"//{parsed.netloc}{path}"
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        return path[1:]
    return path
