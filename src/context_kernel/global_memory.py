from __future__ import annotations

from pathlib import Path
from typing import Any

from .memory import MemoryStore
from .storage import Workspace


def global_workspace(root: Path | None = None) -> Workspace:
    workspace = Workspace(root or Path.home() / ".context-kernel" / "global")
    workspace.init()
    return workspace


def push_global_memories(
    workspace: Workspace,
    *,
    kind: str | None = None,
    global_root: Path | None = None,
) -> dict[str, Any]:
    source = MemoryStore(workspace)
    target = MemoryStore(global_workspace(global_root))
    pushed = []
    project_tag = f"source:{workspace.root.name}"
    for record in source.all(kind=kind):
        tags = sorted(set(record.tags).union({"global", project_tag}))
        copied = target.add(record.kind, record.text, tags=tags)
        pushed.append(copied.to_dict())
    return {
        "direction": "push",
        "source": str(workspace.root),
        "target": str(global_workspace(global_root).root),
        "count": len(pushed),
        "records": pushed,
    }


def pull_global_memories(
    workspace: Workspace,
    *,
    kind: str | None = None,
    limit: int | None = None,
    global_root: Path | None = None,
) -> dict[str, Any]:
    source = MemoryStore(global_workspace(global_root))
    target = MemoryStore(workspace)
    records = source.all(kind=kind)
    if limit is not None:
        records = records[: max(0, limit)]
    pulled = []
    for record in records:
        tags = sorted(set(record.tags).union({"global"}))
        copied = target.add(record.kind, record.text, tags=tags)
        pulled.append(copied.to_dict())
    return {
        "direction": "pull",
        "source": str(global_workspace(global_root).root),
        "target": str(workspace.root),
        "count": len(pulled),
        "records": pulled,
    }
