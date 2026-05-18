from __future__ import annotations

import json
from typing import Any


def parse_json_object(text: str, *, label: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return data


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))


def print_mcp_call_result(result: dict[str, Any]) -> None:
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                print(str(item.get("text", "")))
            else:
                print(json.dumps(item, ensure_ascii=False))
        return
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
