from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .storage import DEFAULT_COMMAND_POLICY, Workspace


FILE_OPERATIONS = {"read", "write", "delete"}
DESTRUCTIVE_FILE_OPERATIONS = {"delete"}
SENSITIVE_FILENAMES = {".env", ".env.example"}
PROTECTED_DIRECTORIES = {".venv", "__pycache__"}
PROTECTED_STATE_FILES = {"memory.sqlite3", "config.json"}
DESTRUCTIVE_COMMAND_TERMS = {
    "del",
    "erase",
    "format",
    "git checkout",
    "git clean",
    "git reset",
    "move-item",
    "rd",
    "remove-item",
    "rm",
    "rmdir",
}
SAFE_COMMAND_ROOTS = set(DEFAULT_COMMAND_POLICY["allowed_roots"])
DANGEROUS_REQUEST_TERMS = {"delete", "remove", "reset", "wipe", "destroy", "清空", "删除", "重置"}


def check_file_policy(
    workspace: Workspace,
    operation: str,
    target: Path | str,
    *,
    allow_destructive: bool = False,
) -> dict[str, Any]:
    operation = operation.lower()
    if operation not in FILE_OPERATIONS:
        raise ValueError(f"Unsupported file operation: {operation}. Expected one of: {', '.join(sorted(FILE_OPERATIONS))}")

    target_path = Path(target)
    resolved = resolve_target(workspace, target_path)
    reasons: list[str] = []
    allowed = True

    if not is_relative_to(resolved, workspace.root):
        allowed = False
        reasons.append("target is outside the workspace root")

    if target_path.name.lower() in SENSITIVE_FILENAMES or resolved.name.lower() in SENSITIVE_FILENAMES:
        allowed = False
        reasons.append("target is a sensitive environment file")

    relative_parts = {part.lower() for part in safe_relative_parts(workspace, resolved)}
    if relative_parts.intersection(PROTECTED_DIRECTORIES):
        allowed = False
        reasons.append("target is inside a protected generated directory")

    if ".akernel" in relative_parts and (operation != "read" or resolved.name in PROTECTED_STATE_FILES):
        allowed = False
        reasons.append("target touches protected Context Kernel state")

    if operation in DESTRUCTIVE_FILE_OPERATIONS and not allow_destructive:
        allowed = False
        reasons.append("destructive file operation requires --allow-destructive")

    return policy_result(
        kind="file",
        allowed=allowed,
        subject=str(resolved),
        operation=operation,
        reasons=reasons,
    )


def check_batch_file_policy(
    workspace: Workspace,
    edits: list[dict[str, Any]],
    *,
    allow_destructive: bool = False,
) -> dict[str, Any]:
    if not edits:
        raise ValueError("Batch file policy requires at least one edit.")

    allowed = True
    reasons: list[str] = []
    items: list[dict[str, Any]] = []
    subjects: list[str] = []
    for index, edit in enumerate(edits, start=1):
        path = edit.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"Batch edit {index} is missing a valid path.")
        policy = check_file_policy(
            workspace,
            "write",
            path,
            allow_destructive=allow_destructive,
        )
        items.append(policy)
        subjects.append(str(policy["subject"]))
        if not policy["allowed"]:
            allowed = False
            reasons.extend([f"edit {index}: {reason}" for reason in policy["reasons"]])

    result = policy_result(
        kind="batch_file",
        allowed=allowed,
        subject=", ".join(subjects),
        operation="batch_patch",
        reasons=reasons,
    )
    result["items"] = items
    return result


def check_command_policy(
    command: str,
    *,
    workspace: Workspace | None = None,
    allow_destructive: bool = False,
) -> dict[str, Any]:
    normalized = " ".join(command.strip().split())
    if not normalized:
        raise ValueError("Command cannot be empty.")

    lower = normalized.casefold()
    reasons: list[str] = []
    allowed = True
    root = command_root(normalized)
    root_candidates = command_root_candidates(normalized)
    command_policy = command_policy_settings(workspace)
    allowed_roots = set(command_policy["allowed_roots"])
    blocked_terms = set(command_policy["blocked_terms"])

    if root and not allowed_roots.intersection(root_candidates):
        allowed = False
        reasons.append(f"command root is not in the workspace safe list: {root}")

    matched_destructive = matched_destructive_terms(lower, extra_terms=blocked_terms)
    if matched_destructive and not allow_destructive:
        allowed = False
        reasons.append("destructive command term requires --allow-destructive: " + ", ".join(matched_destructive))

    if root == "git" and ("--hard" in lower or "-f" in lower) and not allow_destructive:
        allowed = False
        reasons.append("forceful git operations are blocked by default")

    result = policy_result(
        kind="command",
        allowed=allowed,
        subject=normalized,
        operation="execute",
        reasons=reasons,
    )
    result["policy_config"] = {
        "allowed_roots": command_policy["allowed_roots"],
        "blocked_terms": command_policy["blocked_terms"],
        "workspace": str(workspace.root) if workspace else None,
    }
    return result


def assess_request_policy(workspace: Workspace, request: str) -> dict[str, Any]:
    lower = request.casefold()
    warnings: list[str] = []
    if any(term in lower for term in DANGEROUS_REQUEST_TERMS):
        warnings.append("request contains destructive language; require an explicit policy check before file or command execution")
    if ".env" in lower:
        warnings.append("request mentions environment files; avoid exposing secrets and require policy review")
    if "git reset" in lower or "reset --hard" in lower:
        warnings.append("request mentions forceful git reset; default command policy blocks this")
    return {
        "workspace": str(workspace.root),
        "warnings": warnings,
        "requires_policy_check": bool(warnings),
        "command_policy": summarize_command_policy(workspace),
    }


def resolve_target(workspace: Workspace, target: Path) -> Path:
    if target.is_absolute():
        return target.resolve()
    return (workspace.root / target).resolve()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def safe_relative_parts(workspace: Workspace, path: Path) -> list[str]:
    if not is_relative_to(path, workspace.root):
        return []
    return list(path.relative_to(workspace.root).parts)


def command_root(command: str) -> str:
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    return parts[0].strip('"').strip("'").casefold()


def command_root_candidates(command: str) -> set[str]:
    root = command_root(command)
    if not root:
        return set()
    candidates = {root}
    basename = Path(root).name.casefold()
    if basename:
        candidates.add(basename)
        if basename.endswith(".exe"):
            candidates.add(basename[:-4])
    return candidates


def matched_destructive_terms(lower_command: str, *, extra_terms: set[str] | None = None) -> list[str]:
    tokens = {part.strip('"').strip("'").casefold() for part in lower_command.split()}
    destructive_terms = set(DESTRUCTIVE_COMMAND_TERMS)
    destructive_terms.update(extra_terms or set())
    matches: list[str] = []
    for term in sorted(destructive_terms):
        if " " in term:
            if term in lower_command:
                matches.append(term)
            continue
        if term in tokens:
            matches.append(term)
    return matches


def command_policy_settings(workspace: Workspace | None = None) -> dict[str, list[str]]:
    if workspace is None:
        return {
            "allowed_roots": sorted(SAFE_COMMAND_ROOTS),
            "blocked_terms": [],
        }
    config = workspace.load_config()
    command_policy = config.get("command_policy", {})
    return {
        "allowed_roots": list(command_policy.get("allowed_roots", [])),
        "blocked_terms": list(command_policy.get("blocked_terms", [])),
    }


def summarize_command_policy(workspace: Workspace | None = None) -> dict[str, Any]:
    settings = command_policy_settings(workspace)
    return {
        "allowed_roots": settings["allowed_roots"],
        "blocked_terms": settings["blocked_terms"],
        "allowed_root_count": len(settings["allowed_roots"]),
    }


def policy_result(kind: str, allowed: bool, subject: str, operation: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "kind": kind,
        "operation": operation,
        "subject": subject,
        "allowed": allowed,
        "status": "allowed" if allowed else "blocked",
        "reasons": reasons or ["policy checks passed"],
    }
