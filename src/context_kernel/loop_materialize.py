from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .tools import ToolExecutor


def materialize_code_response_if_needed(
    executor: ToolExecutor,
    request: str,
    response_text: str,
) -> dict[str, Any] | None:
    if not looks_like_code_artifact_request(request):
        return None
    blocks = extract_response_code_blocks(response_text)
    if not blocks:
        return None
    paths = infer_code_block_paths(request, response_text, blocks)
    traces: list[dict[str, Any]] = []
    written_paths: list[str] = []
    for block, path in zip(blocks, paths):
        trace_result = executor.write_file(path, block["code"])
        traces.append(trace_result)
        if trace_result.get("ok"):
            written_paths.append(str(trace_result.get("output", {}).get("path", path)))
    if not written_paths:
        return None
    path_lines = "\n".join(f"- {path}" for path in written_paths)
    return {
        "paths": written_paths,
        "traces": traces,
        "message": f"Wrote code to file(s):\n{path_lines}\n\nYou can ask me to modify, run, or verify these files next.",
    }


def looks_like_code_artifact_request(request: str) -> bool:
    text = request.casefold()
    terms = [
        "write code",
        "create code",
        "generate code",
        "implement",
        "script",
        "program",
        "app",
        "streamlit",
        "python",
        "javascript",
        "typescript",
        "\u5199\u4ee3\u7801",
        "\u521b\u5efa",
        "\u751f\u6210",
        "\u5b9e\u73b0",
        "\u5f00\u53d1",
        "\u811a\u672c",
        "\u7a0b\u5e8f",
        "\u7f51\u9875",
        "\u4ee3\u7801\u6587\u4ef6",
        "\u5bfc\u51fa",
        "excel",
    ]
    return any(term in text for term in terms)


def extract_response_code_blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for match in re.finditer(r"(?s)```([A-Za-z0-9_+.-]*)\s*\n(.*?)```", text):
        language = match.group(1).strip().casefold()
        code = match.group(2).strip("\n")
        if not code.strip():
            continue
        if language in {"text", "txt", "console", "terminal", "output"}:
            continue
        if looks_like_run_instruction_block(language, code):
            continue
        blocks.append({"language": language, "code": code})
    return blocks


def looks_like_run_instruction_block(language: str, code: str) -> bool:
    if language not in {"bash", "sh", "shell", "powershell", "ps1", "cmd", "bat"}:
        return False
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    if len(lines) > 4:
        return False
    run_prefixes = (
        "python ",
        "python3 ",
        "pip ",
        "npm ",
        "pnpm ",
        "yarn ",
        "streamlit ",
        "pytest",
        "uv ",
        "node ",
    )
    return all(line.casefold().startswith(run_prefixes) for line in lines)


def infer_code_block_paths(request: str, response_text: str, blocks: list[dict[str, str]]) -> list[str]:
    explicit_paths = extract_code_paths(request + "\n" + response_text)
    paths: list[str] = []
    for index, block in enumerate(blocks, start=1):
        if index <= len(explicit_paths):
            paths.append(explicit_paths[index - 1])
            continue
        extension = extension_for_code_block(block)
        base = slug_from_text(request) or "generated_code"
        suffix = "" if len(blocks) == 1 else f"_{index}"
        paths.append(f"generated/{base}{suffix}{extension}")
    return dedupe_paths(paths)


def extract_code_paths(text: str) -> list[str]:
    pattern = r"(?<![\w./\\-])([A-Za-z0-9_./\\-]+\.(?:py|js|ts|tsx|jsx|html|css|json|md|sh|ps1|sql|yaml|yml|toml|java|go|rs|cpp|c|cs))"
    paths: list[str] = []
    for match in re.finditer(pattern, text):
        path = match.group(1).replace("\\", "/").strip("./")
        if path and not path.startswith(".akernel/") and path not in paths:
            paths.append(path)
    return paths[:8]


def extension_for_code_block(block: dict[str, str]) -> str:
    language = block.get("language", "")
    code = block.get("code", "")
    mapping = {
        "python": ".py",
        "py": ".py",
        "javascript": ".js",
        "js": ".js",
        "typescript": ".ts",
        "ts": ".ts",
        "tsx": ".tsx",
        "jsx": ".jsx",
        "html": ".html",
        "css": ".css",
        "json": ".json",
        "bash": ".sh",
        "sh": ".sh",
        "powershell": ".ps1",
        "sql": ".sql",
    }
    if language in mapping:
        return mapping[language]
    if re.search(r"(?m)^\s*(import |from .* import |def |class )", code):
        return ".py"
    if "streamlit" in code.casefold() or "pandas" in code.casefold():
        return ".py"
    return ".txt"


def slug_from_text(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.casefold())
    stop = {"write", "create", "generate", "implement", "code", "script", "file", "with", "and", "the", "a", "an"}
    selected = [word for word in words if word not in stop][:6]
    return "_".join(selected)


def dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        candidate = path
        stem = str(Path(path).with_suffix(""))
        suffix = Path(path).suffix
        counter = 2
        while candidate in seen:
            candidate = f"{stem}_{counter}{suffix}"
            counter += 1
        seen.add(candidate)
        result.append(candidate)
    return result
