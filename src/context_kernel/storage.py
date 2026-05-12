from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Iterable


STATE_DIR = ".akernel"
DEFAULT_CONFIG_VERSION = 2
DEFAULT_RUNTIME_INSTRUCTIONS = [
    "Use the smallest context packet that can answer the request.",
    "Prefer structured memory and skill contracts over full history.",
    "Report budget pressure and omitted context explicitly.",
]
DEFAULT_COMMAND_POLICY = {
    "allowed_roots": [
        "akernel",
        "akernel.exe",
        "git",
        "py",
        "pytest",
        "python",
        "python.exe",
    ],
    "blocked_terms": [],
}
DEFAULT_CONFIG = {
    "version": DEFAULT_CONFIG_VERSION,
    "default_budget": 1200,
    "runtime_instructions": DEFAULT_RUNTIME_INSTRUCTIONS,
    "command_policy": DEFAULT_COMMAND_POLICY,
}


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state = self.root / STATE_DIR
        self.skills_dir = self.state / "skills"
        self.traces_dir = self.state / "traces"
        self.tool_traces_dir = self.state / "tool_traces"
        self.agent_runs_dir = self.state / "agent_runs"
        self.tasks_dir = self.state / "tasks"
        self.evals_dir = self.state / "evals"
        self.benchmarks_dir = self.state / "benchmarks"
        self.memory_file = self.state / "memory.jsonl"
        self.memory_db = self.state / "memory.sqlite3"
        self.config_file = self.state / "config.json"
        self.project_file = self.state / "project.json"

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.tool_traces_dir.mkdir(parents=True, exist_ok=True)
        self.agent_runs_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.evals_dir.mkdir(parents=True, exist_ok=True)
        self.benchmarks_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            self.memory_file.write_text("", encoding="utf-8")
        if not self.config_file.exists():
            self.write_json(self.config_file, default_config())

    def require_initialized(self) -> None:
        if not self.state.exists():
            raise FileNotFoundError(
                f"Workspace is not initialized: {self.root}. Run `akernel init {self.root}` first."
            )

    def load_config(self) -> dict[str, Any]:
        if not self.config_file.exists():
            return default_config()
        return normalize_config(self.read_json(self.config_file))

    def save_config(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_config(data)
        self.write_json(self.config_file, normalized)
        return normalized

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows


def default_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG)


def normalize_config(data: dict[str, Any] | None) -> dict[str, Any]:
    merged = merge_dicts(default_config(), data if isinstance(data, dict) else {})
    merged["version"] = max(DEFAULT_CONFIG_VERSION, safe_int(merged.get("version"), DEFAULT_CONFIG_VERSION))
    merged["default_budget"] = safe_int(merged.get("default_budget"), int(DEFAULT_CONFIG["default_budget"]))
    merged["runtime_instructions"] = dedupe_strings(
        merged.get("runtime_instructions"),
        fallback=list(DEFAULT_RUNTIME_INSTRUCTIONS),
    )

    command_policy = merged.get("command_policy")
    if not isinstance(command_policy, dict):
        command_policy = {}
    command_policy["allowed_roots"] = dedupe_strings(
        command_policy.get("allowed_roots"),
        fallback=list(DEFAULT_COMMAND_POLICY["allowed_roots"]),
        casefold=True,
    )
    command_policy["blocked_terms"] = dedupe_strings(
        command_policy.get("blocked_terms"),
        fallback=[],
        casefold=True,
    )
    merged["command_policy"] = command_policy
    return merged


def merge_dicts(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def dedupe_strings(values: Any, *, fallback: list[str], casefold: bool = False) -> list[str]:
    source = values if isinstance(values, list) else fallback
    result: list[str] = []
    seen: set[str] = set()
    for item in source:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        key = text.casefold() if casefold else text
        if key in seen:
            continue
        seen.add(key)
        result.append(text.casefold() if casefold else text)
    return result or list(fallback)


def safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
