from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import utc_now
from .storage import Workspace
from .tokenizer import estimate_tokens


PROJECT_PROFILE_VERSION = 1
IGNORED_DIRS = {
    ".akernel",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".sandbox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
KEY_FILE_NAMES = {
    ".cursorrules",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "README.rst",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "pytest.ini",
    "tox.ini",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "go.mod",
}
INSTRUCTION_FILE_NAMES = [
    "AGENTS.md",
    ".akernel/AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
]
MAX_PROJECT_INSTRUCTION_CHARS = 4000


def scan_project(workspace: Workspace, *, update_config: bool = True) -> dict[str, Any]:
    files = list_project_files(workspace.root)
    file_names = {path.name for path in files}
    suffixes = {path.suffix.lower() for path in files}
    top_level = {path.name for path in workspace.root.iterdir()} if workspace.root.exists() else set()

    languages = detect_languages(file_names, suffixes)
    package_managers = detect_package_managers(file_names)
    commands = detect_commands(workspace.root, file_names, top_level, package_managers)
    command_roots = detect_command_roots(languages, package_managers, commands)
    key_files = detect_key_files(workspace.root, files)
    instructions = detect_project_instructions(workspace.root)

    profile = {
        "version": PROJECT_PROFILE_VERSION,
        "generated_at": utc_now(),
        "root": str(workspace.root),
        "languages": languages,
        "package_managers": package_managers,
        "commands": commands,
        "command_roots": command_roots,
        "key_files": key_files,
        "instructions": instructions,
        "file_summary": {
            "scanned_files": len(files),
            "top_level_entries": sorted(list(top_level))[:40],
        },
        "summary": summarize_project(languages, package_managers, commands, key_files),
    }
    Workspace.write_json(workspace.project_file, profile)
    if update_config:
        extend_command_policy(workspace, command_roots)
    return profile


def load_project_profile(workspace: Workspace) -> dict[str, Any] | None:
    if not workspace.project_file.exists():
        return None
    return Workspace.read_json(workspace.project_file)


def compact_project_profile(profile: dict[str, Any] | None, *, max_tokens: int = 220) -> dict[str, Any] | None:
    if not profile:
        return None
    compact = {
        "summary": profile.get("summary", ""),
        "languages": profile.get("languages", [])[:6],
        "package_managers": profile.get("package_managers", [])[:4],
        "commands": profile.get("commands", {}),
        "key_files": profile.get("key_files", [])[:12],
        "command_roots": profile.get("command_roots", [])[:12],
        "instructions": compact_project_instructions(profile.get("instructions", [])),
    }
    if estimate_tokens(compact) <= max_tokens:
        return compact
    compact["key_files"] = compact["key_files"][:6]
    compact["commands"] = {
        key: value
        for key, value in compact["commands"].items()
        if key in {"test", "lint", "build"}
    }
    compact["instructions"] = compact["instructions"][:2]
    return compact


def list_project_files(root: Path, *, max_files: int = 5000) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    stack = [root]
    while stack and len(files) < max_files:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name in IGNORED_DIRS:
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                files.append(entry)
                if len(files) >= max_files:
                    break
    return files


def detect_languages(file_names: set[str], suffixes: set[str]) -> list[str]:
    languages: list[str] = []
    if {".py"}.intersection(suffixes) or {"pyproject.toml", "setup.py", "requirements.txt"}.intersection(file_names):
        languages.append("python")
    if {".js", ".jsx", ".ts", ".tsx"}.intersection(suffixes) or "package.json" in file_names:
        languages.append("javascript/typescript")
    if ".rs" in suffixes or "Cargo.toml" in file_names:
        languages.append("rust")
    if ".go" in suffixes or "go.mod" in file_names:
        languages.append("go")
    if ".java" in suffixes or {"pom.xml", "build.gradle", "build.gradle.kts"}.intersection(file_names):
        languages.append("java")
    return languages or ["unknown"]


def detect_package_managers(file_names: set[str]) -> list[str]:
    managers: list[str] = []
    if "pyproject.toml" in file_names:
        managers.append("python/pyproject")
    if "requirements.txt" in file_names:
        managers.append("python/pip")
    if "package.json" in file_names:
        if "pnpm-lock.yaml" in file_names:
            managers.append("node/pnpm")
        elif "yarn.lock" in file_names:
            managers.append("node/yarn")
        else:
            managers.append("node/npm")
    if "Cargo.toml" in file_names:
        managers.append("rust/cargo")
    if "go.mod" in file_names:
        managers.append("go")
    return managers


def detect_commands(
    root: Path,
    file_names: set[str],
    top_level: set[str],
    package_managers: list[str],
) -> dict[str, str]:
    commands: dict[str, str] = {}
    if "pyproject.toml" in file_names or "requirements.txt" in file_names or "setup.py" in file_names:
        if "pytest.ini" in file_names or any(path.name.startswith("test_") and path.suffix == ".py" for path in list_project_files(root / "tests", max_files=200)):
            commands["test"] = "python -m pytest"
        elif "tests" in top_level:
            commands["test"] = "python -m unittest discover -s tests"
        commands["install"] = "python -m pip install -e ."

    package_json = root / "package.json"
    if package_json.exists():
        package_data = read_package_json(package_json)
        scripts = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
        runner = "pnpm" if "node/pnpm" in package_managers else "yarn" if "node/yarn" in package_managers else "npm"
        if isinstance(scripts, dict):
            if "test" in scripts:
                commands.setdefault("test", f"{runner} test")
            if "lint" in scripts:
                commands["lint"] = f"{runner} run lint"
            if "build" in scripts:
                commands["build"] = f"{runner} run build"
        commands.setdefault("install_node", f"{runner} install")

    if "Cargo.toml" in file_names:
        commands.setdefault("test", "cargo test")
        commands.setdefault("build", "cargo build")
    if "go.mod" in file_names:
        commands.setdefault("test", "go test ./...")
        commands.setdefault("build", "go build ./...")
    return commands


def detect_command_roots(languages: list[str], package_managers: list[str], commands: dict[str, str]) -> list[str]:
    roots = {"akernel", "akernel.exe", "git"}
    if "python" in languages or any(manager.startswith("python/") for manager in package_managers):
        roots.update({"py", "pytest", "python", "python.exe"})
    if any(manager.startswith("node/") for manager in package_managers):
        roots.update({"node", "node.exe", "npm", "npm.cmd", "npx", "npx.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"})
    if "rust" in languages:
        roots.update({"cargo", "cargo.exe", "rustc", "rustc.exe"})
    if "go" in languages:
        roots.update({"go", "go.exe"})
    for command in commands.values():
        root = command.split()[0].strip().casefold()
        if root:
            roots.add(root)
    return sorted(roots)


def detect_key_files(root: Path, files: list[Path]) -> list[str]:
    key_files: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        if path.name in KEY_FILE_NAMES or relative.startswith(".github/workflows/"):
            key_files.append(relative)
    return sorted(key_files)[:24]


def detect_project_instructions(root: Path) -> list[dict[str, Any]]:
    instructions: list[dict[str, Any]] = []
    for name in INSTRUCTION_FILE_NAMES:
        path = root / name
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        content = text.strip()
        if not content:
            continue
        instructions.append(
            {
                "path": name.replace("\\", "/"),
                "content": content[:MAX_PROJECT_INSTRUCTION_CHARS],
                "truncated": len(content) > MAX_PROJECT_INSTRUCTION_CHARS,
                "estimated_tokens": estimate_tokens(content[:MAX_PROJECT_INSTRUCTION_CHARS]),
            }
        )
    return instructions


def compact_project_instructions(instructions: Any) -> list[dict[str, Any]]:
    if not isinstance(instructions, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in instructions[:3]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "path": str(item.get("path", "")),
                "content": str(item.get("content", ""))[:1800],
                "truncated": bool(item.get("truncated", False)),
            }
        )
    return compact


def extend_command_policy(workspace: Workspace, command_roots: list[str]) -> None:
    config = workspace.load_config()
    policy = config.setdefault("command_policy", {})
    existing = policy.get("allowed_roots", [])
    merged = []
    seen: set[str] = set()
    for root in list(existing) + command_roots:
        if not isinstance(root, str) or not root.strip():
            continue
        normalized = root.strip().casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    policy["allowed_roots"] = merged
    workspace.save_config(config)


def read_package_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def summarize_project(
    languages: list[str],
    package_managers: list[str],
    commands: dict[str, str],
    key_files: list[str],
) -> str:
    language_text = ", ".join(languages)
    manager_text = ", ".join(package_managers) if package_managers else "none"
    command_text = ", ".join(f"{name}=`{command}`" for name, command in commands.items()) or "none"
    key_text = ", ".join(key_files[:6]) if key_files else "none"
    return f"languages={language_text}; package_managers={manager_text}; commands={command_text}; key_files={key_text}"
