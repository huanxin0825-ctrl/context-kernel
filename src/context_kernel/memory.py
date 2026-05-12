from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from uuid import uuid4

from .models import MemoryRecord, SelectedMemory, utc_now
from .storage import Workspace
from .text import matched_terms
from .tokenizer import estimate_tokens


ALLOWED_KINDS = {"fact", "preference", "project_state", "task_state", "decision"}
STRONG_MEMORY_TERMS = {
    "agent",
    "architecture",
    "budget",
    "cli",
    "context",
    "eval",
    "kernel",
    "memory",
    "mvp",
    "phase2",
    "prototype",
    "routing",
    "runtime",
    "skill",
    "token",
    "tokens",
}


class MemoryStore:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self._ensure_schema()
        self._migrate_jsonl_if_needed()

    def add(self, kind: str, text: str, tags: list[str] | None = None) -> MemoryRecord:
        validate_kind(kind)
        clean_text = normalize_text(text)
        clean_tags = normalize_tags(tags or [])
        text_hash = memory_hash(kind, clean_text)
        existing = self._active_by_hash(kind, text_hash)
        if existing:
            merged_tags = sorted(set(existing.tags).union(clean_tags))
            if merged_tags != existing.tags:
                return self.update(existing.id, tags=merged_tags)
            return existing

        now = utc_now()
        record = MemoryRecord(
            id=uuid4().hex[:12],
            kind=kind,
            text=clean_text,
            tags=clean_tags,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO memories(id, kind, text, tags, text_hash, created_at, updated_at, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    record.id,
                    record.kind,
                    record.text,
                    json.dumps(record.tags, ensure_ascii=False),
                    text_hash,
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def get(self, record_id: str, *, include_archived: bool = False) -> MemoryRecord:
        with self._connect() as db:
            if include_archived:
                row = db.execute("SELECT * FROM memories WHERE id = ?", (record_id,)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM memories WHERE id = ? AND archived_at IS NULL",
                    (record_id,),
                ).fetchone()
        if row is None:
            raise KeyError(f"Memory record not found: {record_id}")
        return record_from_row(row)

    def update(
        self,
        record_id: str,
        *,
        kind: str | None = None,
        text: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        current = self.get(record_id)
        next_kind = kind or current.kind
        validate_kind(next_kind)
        next_text = normalize_text(text) if text is not None else current.text
        next_tags = normalize_tags(tags) if tags is not None else current.tags
        next_hash = memory_hash(next_kind, next_text)
        duplicate = self._active_by_hash(next_kind, next_hash)
        if duplicate and duplicate.id != record_id:
            raise ValueError(f"Update would duplicate existing memory: {duplicate.id}")

        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                UPDATE memories
                SET kind = ?, text = ?, tags = ?, text_hash = ?, updated_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (
                    next_kind,
                    next_text,
                    json.dumps(next_tags, ensure_ascii=False),
                    next_hash,
                    now,
                    record_id,
                ),
            )
        return self.get(record_id)

    def forget(self, record_id: str) -> bool:
        now = utc_now()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE memories SET archived_at = ?, updated_at = ? WHERE id = ? AND archived_at IS NULL",
                (now, now, record_id),
            )
        return cursor.rowcount > 0

    def all(self, kind: str | None = None, *, include_archived: bool = False) -> list[MemoryRecord]:
        if kind:
            validate_kind(kind)
        where: list[str] = []
        params: list[str] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if not include_archived:
            where.append("archived_at IS NULL")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM memories {clause} ORDER BY created_at ASC, id ASC",
                params,
            ).fetchall()
        return [record_from_row(row) for row in rows]

    def search(self, query: str, kind: str | None = None, limit: int = 5, budget_tokens: int | None = None) -> list[SelectedMemory]:
        selected: list[SelectedMemory] = []
        records = self.all(kind)
        total = len(records)
        for index, record in enumerate(records):
            haystack = " ".join([record.kind, record.text, " ".join(record.tags)])
            matches = matched_terms(query, haystack)
            strong_matches = sorted(set(matches).intersection(STRONG_MEMORY_TERMS))
            if is_relevant_memory_match(matches, strong_matches):
                recency_bonus = max(0, min(3, total - index - 1))
                kind_bonus = 2 if kind and record.kind == kind else 0
                score = len(matches) * 10 + len(strong_matches) * 8 + recency_bonus + kind_bonus
                reason_parts = [f"matched terms: {', '.join(matches)}"]
                if strong_matches:
                    reason_parts.append(f"strong terms: {', '.join(strong_matches)}")
                if recency_bonus:
                    reason_parts.append(f"recency bonus: {recency_bonus}")
                if kind_bonus:
                    reason_parts.append(f"kind filter bonus: {kind_bonus}")
                selected.append(
                    SelectedMemory(
                        record=record,
                        score=score,
                        reason="; ".join(reason_parts),
                        matched_terms=matches,
                    )
                )

        selected = sorted(selected, key=lambda item: item.score, reverse=True)[:limit]
        if budget_tokens is None:
            return selected

        packed: list[SelectedMemory] = []
        remaining = budget_tokens
        for item in selected:
            cost = estimate_tokens(item.record.to_dict())
            if cost <= remaining:
                packed.append(item)
                remaining -= cost
        return packed

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.workspace.state.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.workspace.memory_db)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _ensure_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(kind, text_hash)")

    def _migrate_jsonl_if_needed(self) -> None:
        if not self.workspace.memory_file.exists():
            return
        rows = Workspace.read_jsonl(self.workspace.memory_file)
        if not rows:
            return
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
        if count:
            return
        for row in rows:
            record = MemoryRecord.from_dict(row)
            validate_kind(record.kind)
            self._insert_migrated(record)

    def _insert_migrated(self, record: MemoryRecord) -> None:
        clean_text = normalize_text(record.text)
        clean_tags = normalize_tags(record.tags)
        text_hash = memory_hash(record.kind, clean_text)
        if self._active_by_hash(record.kind, text_hash):
            return
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO memories(id, kind, text, tags, text_hash, created_at, updated_at, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.kind,
                    clean_text,
                    json.dumps(clean_tags, ensure_ascii=False),
                    text_hash,
                    record.created_at,
                    record.updated_at,
                    record.archived_at,
                ),
            )

    def _active_by_hash(self, kind: str, text_hash: str) -> MemoryRecord | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM memories
                WHERE kind = ? AND text_hash = ? AND archived_at IS NULL
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (kind, text_hash),
            ).fetchone()
        return record_from_row(row) if row else None


def validate_kind(kind: str) -> None:
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"Unsupported memory kind: {kind}. Expected one of: {', '.join(sorted(ALLOWED_KINDS))}")


def normalize_text(text: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("Memory text cannot be empty.")
    return normalized


def normalize_tags(tags: list[str] | None) -> list[str]:
    return sorted({tag.strip() for tag in tags or [] if tag.strip()})


def memory_hash(kind: str, text: str) -> str:
    canonical = f"{kind}\n{' '.join(text.split()).casefold()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def record_from_row(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=str(row["id"]),
        kind=str(row["kind"]),
        text=str(row["text"]),
        tags=list(json.loads(row["tags"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row["archived_at"],
    )


def is_relevant_memory_match(matches: list[str], strong_matches: list[str]) -> bool:
    if len(matches) >= 2:
        return True
    return bool(strong_matches)
