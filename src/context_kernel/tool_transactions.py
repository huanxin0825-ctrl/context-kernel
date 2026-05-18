from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .policy import resolve_target
from .storage import Workspace


@dataclass
class FileSnapshot:
    path: Path
    exists: bool
    is_file: bool
    content: str


class FileTransaction:
    def __init__(self, *, label: str, snapshots: list[FileSnapshot]):
        self.id = uuid4().hex[:12]
        self.label = label
        self.snapshots = snapshots

    def commit_output(self) -> dict[str, Any]:
        return self._output(status="committed", rolled_back=False)

    def rollback_output(self) -> dict[str, Any]:
        restored: list[str] = []
        deleted: list[str] = []
        for snapshot in self.snapshots:
            if snapshot.exists and snapshot.is_file:
                snapshot.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_restore_text(snapshot.path, snapshot.content)
                restored.append(str(snapshot.path))
            elif snapshot.path.exists() and snapshot.path.is_file():
                snapshot.path.unlink()
                deleted.append(str(snapshot.path))
        return self._output(
            status="rolled_back",
            rolled_back=True,
            rollback={"restored": restored, "deleted": deleted},
        )

    def _output(
        self,
        *,
        status: str,
        rolled_back: bool,
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "status": status,
            "rolled_back": rolled_back,
            "snapshot_count": len(self.snapshots),
            "paths": [str(snapshot.path) for snapshot in self.snapshots],
        }
        if rollback is not None:
            output["rollback"] = rollback
        return output


def begin_file_transaction(workspace: Workspace, paths: list[str], *, label: str) -> FileTransaction:
    snapshots: list[FileSnapshot] = []
    seen: set[str] = set()
    for path in paths:
        target = resolve_target(workspace, Path(path))
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        exists = target.exists()
        is_file = exists and target.is_file()
        snapshots.append(
            FileSnapshot(
                path=target,
                exists=exists,
                is_file=is_file,
                content=target.read_text(encoding="utf-8", errors="replace") if is_file else "",
            )
        )
    return FileTransaction(label=label, snapshots=snapshots)


def atomic_restore_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(text)
    try:
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
