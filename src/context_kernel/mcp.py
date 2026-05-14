from __future__ import annotations

from copy import deepcopy
import re
import shlex
from typing import Any

from .models import utc_now
from .storage import Workspace
from .tokenizer import estimate_tokens


MCP_CONFIG_VERSION = 1
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def default_mcp_config() -> dict[str, Any]:
    return {"version": MCP_CONFIG_VERSION, "servers": {}}


def load_mcp_config(workspace: Workspace) -> dict[str, Any]:
    if not workspace.mcp_file.exists():
        return default_mcp_config()
    return normalize_mcp_config(Workspace.read_json(workspace.mcp_file))


def save_mcp_config(workspace: Workspace, config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_mcp_config(config)
    Workspace.write_json(workspace.mcp_file, normalized)
    return normalized


def normalize_mcp_config(config: dict[str, Any] | None) -> dict[str, Any]:
    data = deepcopy(config) if isinstance(config, dict) else {}
    servers = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    normalized_servers: dict[str, Any] = {}
    for name, server in servers.items():
        if not isinstance(server, dict) or not is_valid_mcp_name(str(name)):
            continue
        normalized = normalize_mcp_server(str(name), server)
        normalized_servers[str(name)] = normalized
    return {"version": MCP_CONFIG_VERSION, "servers": normalized_servers}


def normalize_mcp_server(name: str, server: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    tools = server.get("tools")
    return {
        "name": name,
        "transport": "stdio",
        "command": str(server.get("command", "")).strip(),
        "cwd": str(server.get("cwd", "")).strip(),
        "enabled": bool(server.get("enabled", True)),
        "tools": normalize_tool_summaries(tools),
        "created_at": str(server.get("created_at") or now),
        "updated_at": str(server.get("updated_at") or now),
    }


def normalize_tool_summaries(tools: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    source = tools if isinstance(tools, list) else []
    for item in source:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        if not name:
            continue
        result.append({"name": name, "description": description})
    return result


def parse_tool_summary(value: str) -> dict[str, str]:
    name, separator, description = value.partition(":")
    if not separator:
        name, _, description = value.partition("=")
    name = name.strip()
    if not name:
        raise ValueError("MCP tool summary must include a tool name.")
    return {"name": name, "description": description.strip()}


def is_valid_mcp_name(name: str) -> bool:
    return bool(MCP_NAME_RE.fullmatch(name))


def add_mcp_server(
    workspace: Workspace,
    name: str,
    *,
    command: str,
    cwd: str = "",
    tools: list[str] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    if not is_valid_mcp_name(name):
        raise ValueError("MCP server name must use letters, numbers, dot, dash, or underscore.")
    if not command.strip():
        raise ValueError("MCP server command is required.")
    config = load_mcp_config(workspace)
    now = utc_now()
    existing = config["servers"].get(name, {})
    config["servers"][name] = normalize_mcp_server(
        name,
        {
            "command": command,
            "cwd": cwd,
            "enabled": enabled,
            "tools": [parse_tool_summary(item) for item in (tools or [])],
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
        },
    )
    save_mcp_config(workspace, config)
    return config["servers"][name]


def list_mcp_servers(workspace: Workspace, *, include_disabled: bool = True) -> list[dict[str, Any]]:
    servers = load_mcp_config(workspace)["servers"]
    result = list(servers.values())
    if not include_disabled:
        result = [server for server in result if server.get("enabled")]
    return sorted(result, key=lambda item: str(item.get("name", "")))


def get_mcp_server(workspace: Workspace, name: str) -> dict[str, Any]:
    servers = load_mcp_config(workspace)["servers"]
    if name not in servers:
        raise KeyError(f"Unknown MCP server: {name}")
    return servers[name]


def remove_mcp_server(workspace: Workspace, name: str) -> dict[str, Any]:
    config = load_mcp_config(workspace)
    if name not in config["servers"]:
        raise KeyError(f"Unknown MCP server: {name}")
    removed = config["servers"].pop(name)
    save_mcp_config(workspace, config)
    return removed


def set_mcp_server_enabled(workspace: Workspace, name: str, enabled: bool) -> dict[str, Any]:
    config = load_mcp_config(workspace)
    if name not in config["servers"]:
        raise KeyError(f"Unknown MCP server: {name}")
    server = dict(config["servers"][name])
    server["enabled"] = enabled
    server["updated_at"] = utc_now()
    config["servers"][name] = normalize_mcp_server(name, server)
    save_mcp_config(workspace, config)
    return config["servers"][name]


def mcp_context_summary(workspace: Workspace, *, budget_tokens: int = 220) -> dict[str, Any]:
    servers = list_mcp_servers(workspace, include_disabled=False)
    summaries: list[dict[str, Any]] = []
    for server in servers:
        item = {
            "name": server["name"],
            "transport": server["transport"],
            "command_root": command_root(str(server.get("command", ""))),
            "tools": server.get("tools", [])[:8],
        }
        if server.get("cwd"):
            item["cwd"] = server["cwd"]
        if estimate_tokens({"servers": summaries + [item]}) > budget_tokens:
            break
        summaries.append(item)
    return {
        "enabled_count": len(servers),
        "servers": summaries,
        "omitted_count": max(0, len(servers) - len(summaries)),
        "policy": "MCP config is summarized only; tool schemas and calls are loaded on demand.",
    }


def command_root(command: str) -> str:
    if not command.strip():
        return ""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""
