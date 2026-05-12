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
PINNED_TAGS = {"keep", "pinned", "global"}
RECOVERABLE_KINDS = {"task_state", "project_state"}
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

    def prune(
        self,
        *,
        max_records: int | None = None,
        max_tokens: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, object]:
        records = self.all()
        if max_records is None and max_tokens is None:
            raise ValueError("memory prune requires --max-records, --max-tokens, or both.")
        if max_records is not None and max_records < 0:
            raise ValueError("--max-records must be >= 0")
        if max_tokens is not None and max_tokens < 0:
            raise ValueError("--max-tokens must be >= 0")

        keep_limit = max_records if max_records is not None else len(records)
        decisions = self.retention_analysis(records)
        ranked = sorted(decisions, key=lambda item: item["retention_key"], reverse=True)
        kept: list[dict[str, object]] = []
        used_tokens = 0
        for decision in ranked:
            cost = int(decision["token_cost"])
            if len(kept) >= keep_limit:
                continue
            if max_tokens is not None and used_tokens + cost > max_tokens:
                continue
            kept.append(decision)
            used_tokens += cost

        kept_ids = {str(decision["record"]["id"]) for decision in kept}
        candidates = [decision for decision in decisions if str(decision["record"]["id"]) not in kept_ids]
        if not dry_run:
            for decision in candidates:
                self.forget(str(decision["record"]["id"]))
        return {
            "dry_run": dry_run,
            "active_before": len(records),
            "kept": len(kept),
            "archived": 0 if dry_run else len(candidates),
            "candidate_count": len(candidates),
            "kept_tokens": used_tokens,
            "active_tokens": sum(int(decision["token_cost"]) for decision in decisions),
            "recoverable_candidates": sum(1 for decision in candidates if decision["recoverability"]["level"] != "none"),
            "candidates": [decision["record"] for decision in candidates],
            "candidate_decisions": [strip_retention_key(decision) for decision in candidates],
            "kept_decisions": [strip_retention_key(decision) for decision in kept],
        }

    def retention_analysis(self, records: list[MemoryRecord] | None = None) -> list[dict[str, object]]:
        active_records = records if records is not None else self.all()
        recovery_index = memory_recovery_index(self.workspace)
        total = max(1, len(active_records))
        decisions: list[dict[str, object]] = []
        for index, record in enumerate(active_records):
            token_cost = estimate_tokens(record.to_dict())
            recoverability = recoverability_summary(record, recovery_index)
            score, reasons = retention_score(record, token_cost, recoverability, index=index, total=total)
            decisions.append(
                {
                    "record": record.to_dict(),
                    "score": score,
                    "token_cost": token_cost,
                    "recoverability": recoverability,
                    "reasons": reasons,
                    "retention_key": (score, -token_cost, record.updated_at, record.id),
                }
            )
        return decisions

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


def memory_retention_key(record: MemoryRecord) -> tuple[int, int, str, str]:
    kind_priority = {
        "preference": 50,
        "decision": 45,
        "fact": 40,
        "project_state": 30,
        "task_state": 20,
    }.get(record.kind, 10)
    tag_priority = 100 if any(tag.casefold() in {"keep", "pinned", "global"} for tag in record.tags) else 0
    text_cost = estimate_tokens(record.to_dict())
    return (tag_priority + kind_priority, -text_cost, record.updated_at, record.id)


def retention_score(
    record: MemoryRecord,
    token_cost: int,
    recoverability: dict[str, object],
    *,
    index: int,
    total: int,
) -> tuple[int, list[str]]:
    kind_score = {
        "preference": 60,
        "decision": 55,
        "fact": 42,
        "project_state": 32,
        "task_state": 18,
    }.get(record.kind, 10)
    reasons = [f"kind:{record.kind}+{kind_score}"]
    score = kind_score

    pinned_tags = sorted(set(tag.casefold() for tag in record.tags).intersection(PINNED_TAGS))
    if pinned_tags:
        score += 100
        reasons.append(f"pinned:{','.join(pinned_tags)}+100")

    recency_score = min(10, max(0, int(((index + 1) / max(1, total)) * 10)))
    if recency_score:
        score += recency_score
        reasons.append(f"recency+{recency_score}")

    if recoverability["level"] != "none":
        if record.kind in RECOVERABLE_KINDS:
            score -= 18
            reasons.append("trace_recoverable-18")
        else:
            score += 4
            reasons.append("trace_linked+4")
    elif record.kind in RECOVERABLE_KINDS:
        score += 8
        reasons.append("not_recoverable+8")

    size_penalty = min(25, max(0, token_cost // 40))
    if size_penalty:
        score -= size_penalty
        reasons.append(f"token_cost-{size_penalty}")
    return score, reasons


def recoverability_summary(record: MemoryRecord, recovery_index: dict[str, list[str]]) -> dict[str, object]:
    sources = recovery_index.get(record.id, [])
    if not sources:
        return {"level": "none", "sources": [], "reason": "No linked trace or task ref was found."}
    level = "high" if record.kind in RECOVERABLE_KINDS else "linked"
    return {
        "level": level,
        "sources": sources[:5],
        "reason": "Record id appears in trace, agent-run, or task references.",
    }


def memory_recovery_index(workspace: Workspace) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for folder, label in [
        (workspace.traces_dir, "trace"),
        (workspace.agent_runs_dir, "agent_run"),
        (workspace.tasks_dir, "task"),
    ]:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            data = safe_read_json(path)
            if not data:
                continue
            source = f"{label}:{data.get('id') or path.stem}"
            for record_id in memory_ids_from_document(data):
                add_recovery_source(index, record_id, source)
    return index


def memory_ids_from_document(data: dict[str, object]) -> list[str]:
    ids: list[str] = []
    state = data.get("state")
    if isinstance(state, dict):
        records = state.get("records", [])
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict) and record.get("id"):
                    ids.append(str(record["id"]))
    refs = data.get("refs")
    if isinstance(refs, dict):
        memories = refs.get("memories", [])
        if isinstance(memories, list):
            ids.extend(str(item) for item in memories if item)
    return ids


def add_recovery_source(index: dict[str, list[str]], record_id: str, source: str) -> None:
    sources = index.setdefault(record_id, [])
    if source not in sources:
        sources.append(source)


def safe_read_json(path) -> dict[str, object] | None:
    try:
        data = Workspace.read_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def strip_retention_key(decision: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in decision.items() if key != "retention_key"}


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
