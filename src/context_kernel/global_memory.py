from __future__ import annotations

from pathlib import Path
from typing import Any

from .memory import MemoryStore
from .storage import Workspace


def global_workspace(root: Path | None = None) -> Workspace:
    workspace = Workspace(root or Path.home() / ".akernel" / "global")
    workspace.init()
    return workspace


def push_global_memories(
    workspace: Workspace,
    *,
    kind: str | None = None,
    namespace: str | None = None,
    tag: str | None = None,
    dry_run: bool = False,
    global_root: Path | None = None,
) -> dict[str, Any]:
    source = MemoryStore(workspace)
    global_ws = global_workspace(global_root)
    target = MemoryStore(global_ws)
    pushed = []
    project_name = workspace.root.name
    sync_namespace = normalize_namespace(namespace or project_name)
    for record in filter_records(source.all(kind=kind), tag=tag):
        tags = sorted(
            set(record.tags).union(
                {
                    "global",
                    f"namespace:{sync_namespace}",
                    f"source:{project_name}",
                    f"source_project:{project_name}",
                    f"source_root:{workspace.root.as_posix()}",
                }
            )
        )
        if dry_run:
            copied = record.to_dict()
            copied["tags"] = tags
        else:
            copied = target.add(record.kind, record.text, tags=tags).to_dict()
        pushed.append(copied)
    return {
        "direction": "push",
        "dry_run": dry_run,
        "namespace": sync_namespace,
        "source": str(workspace.root),
        "target": str(global_ws.root),
        "count": 0 if dry_run else len(pushed),
        "candidate_count": len(pushed),
        "records": pushed,
    }


def pull_global_memories(
    workspace: Workspace,
    *,
    kind: str | None = None,
    namespace: str | None = None,
    source_project: str | None = None,
    tag: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    global_root: Path | None = None,
) -> dict[str, Any]:
    global_ws = global_workspace(global_root)
    source = MemoryStore(global_ws)
    target = MemoryStore(workspace)
    records = filter_records(
        source.all(kind=kind),
        namespace=namespace,
        source_project=source_project,
        tag=tag,
    )
    if limit is not None:
        records = records[: max(0, limit)]
    pulled = []
    for record in records:
        tags = sorted(set(record.tags).union({"global", "imported_global"}))
        if dry_run:
            copied = record.to_dict()
            copied["tags"] = tags
        else:
            copied = target.add(record.kind, record.text, tags=tags).to_dict()
        pulled.append(copied)
    return {
        "direction": "pull",
        "dry_run": dry_run,
        "namespace": normalize_namespace(namespace) if namespace else None,
        "source_project": source_project,
        "source": str(global_ws.root),
        "target": str(workspace.root),
        "count": 0 if dry_run else len(pulled),
        "candidate_count": len(pulled),
        "records": pulled,
    }


def filter_records(
    records: list[Any],
    *,
    namespace: str | None = None,
    source_project: str | None = None,
    tag: str | None = None,
) -> list[Any]:
    filtered = records
    if namespace:
        namespace_tag = f"namespace:{normalize_namespace(namespace)}"
        filtered = [record for record in filtered if namespace_tag in record.tags]
    if source_project:
        source_tags = {f"source_project:{source_project}", f"source:{source_project}"}
        filtered = [record for record in filtered if source_tags.intersection(record.tags)]
    if tag:
        filtered = [record for record in filtered if tag in record.tags]
    return filtered


def normalize_namespace(value: str) -> str:
    normalized = "-".join(str(value).strip().split()).lower()
    return normalized or "default"
