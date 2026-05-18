from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import threading
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

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
    args = server.get("args") if isinstance(server.get("args"), list) else []
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    env_keys = server.get("env_keys") if isinstance(server.get("env_keys"), list) else []
    tool_approvals = server.get("tool_approvals") if isinstance(server.get("tool_approvals"), dict) else {}
    return {
        "name": name,
        "transport": "stdio",
        "command": str(server.get("command", "")).strip(),
        "args": [str(item) for item in args],
        "cwd": str(server.get("cwd", "")).strip(),
        "env": {str(key): str(value) for key, value in env.items() if str(key).strip()},
        "env_keys": sorted({str(item) for item in env_keys if str(item).strip()} | {str(key) for key in env.keys()}),
        "env_required": bool(server.get("env_required", False)),
        "startup_timeout_ms": safe_positive_int(server.get("startup_timeout_ms"), 10000),
        "tool_approvals": normalize_tool_approvals(tool_approvals),
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


def normalize_tool_approvals(tool_approvals: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for name, config in tool_approvals.items():
        if not isinstance(config, dict):
            continue
        tool_name = str(name).strip()
        if not tool_name:
            continue
        approval_mode = str(config.get("approval_mode", "")).strip()
        if approval_mode:
            result[tool_name] = {"approval_mode": approval_mode}
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
    args: list[str] | None = None,
    cwd: str = "",
    env: dict[str, str] | None = None,
    env_keys: list[str] | None = None,
    env_required: bool = False,
    startup_timeout_ms: int | None = None,
    tool_approvals: dict[str, Any] | None = None,
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
    tool_summaries = [parse_tool_summary(item) for item in tools] if tools is not None else existing.get("tools", [])
    config["servers"][name] = normalize_mcp_server(
        name,
        {
            "command": command,
            "args": args or [],
            "cwd": cwd,
            "env": env or {},
            "env_keys": env_keys or [],
            "env_required": env_required,
            "startup_timeout_ms": startup_timeout_ms,
            "tool_approvals": tool_approvals or {},
            "enabled": enabled,
            "tools": tool_summaries,
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


def refresh_mcp_server_tools(workspace: Workspace, name: str, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    server = get_mcp_server(workspace, name)
    if not server.get("enabled", True):
        raise ValueError(f"MCP server is disabled: {name}")
    discovered = discover_mcp_tools(server, timeout_seconds=timeout_seconds)
    config = load_mcp_config(workspace)
    updated = dict(config["servers"][name])
    updated["tools"] = discovered["tools"]
    updated["updated_at"] = utc_now()
    config["servers"][name] = normalize_mcp_server(name, updated)
    save_mcp_config(workspace, config)
    result = dict(config["servers"][name])
    result["discovery"] = {
        "protocol": discovered.get("protocol"),
        "server_info": discovered.get("server_info", {}),
        "tool_count": len(result["tools"]),
    }
    return result


def call_mcp_tool(
    workspace: Workspace,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 10.0,
    allow_unknown: bool = False,
) -> dict[str, Any]:
    server = get_mcp_server(workspace, server_name)
    if not server.get("enabled", True):
        raise ValueError(f"MCP server is disabled: {server_name}")
    known_tools = {tool.get("name") for tool in server.get("tools", []) if isinstance(tool, dict)}
    if known_tools and tool_name not in known_tools and not allow_unknown:
        raise ValueError(f"Unknown MCP tool for {server_name}: {tool_name}. Run `akernel mcp refresh {server_name}` first.")
    result = run_mcp_session(
        server,
        [{"method": "tools/call", "params": {"name": tool_name, "arguments": arguments or {}}}],
        timeout_seconds=effective_mcp_timeout(server, timeout_seconds),
    )[0]
    return {
        "server": server_name,
        "tool": tool_name,
        "arguments": arguments or {},
        "result": result,
    }


def discover_mcp_tools(server: dict[str, Any], *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    initialize, tools_result = run_mcp_session(
        server,
        [{"method": "tools/list", "params": {}}],
        timeout_seconds=effective_mcp_timeout(server, timeout_seconds),
        include_initialize=True,
    )
    return {
        "protocol": initialize.get("protocolVersion"),
        "server_info": initialize.get("serverInfo", {}),
        "tools": normalize_discovered_tools(tools_result.get("tools", [])),
    }


def run_mcp_session(
    server: dict[str, Any],
    calls: list[dict[str, Any]],
    *,
    timeout_seconds: float,
    include_initialize: bool = False,
) -> list[dict[str, Any]]:
    command = str(server.get("command", "")).strip()
    if not command:
        raise ValueError("MCP server command is required.")
    cwd = str(server.get("cwd", "")).strip() or None
    if cwd:
        cwd = str(Path(cwd).resolve())
    process_env = os.environ.copy()
    process_env.update({str(key): str(value) for key, value in (server.get("env") or {}).items()})
    process = subprocess.Popen(
        mcp_command_args(server),
        cwd=cwd,
        env=process_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stderr_lines: list[str] = []
    stderr_thread = threading.Thread(target=_collect_stderr, args=(process, stderr_lines), daemon=True)
    stderr_thread.start()
    client = JsonRpcLineClient(process, timeout_seconds=timeout_seconds)
    try:
        initialize = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "akernel", "version": "0.1"},
            },
        )
        client.notify("notifications/initialized", {})
        results = [client.request(str(call["method"]), call.get("params") or {}) for call in calls]
        return [initialize, *results] if include_initialize else results
    finally:
        terminate_process(process)


class JsonRpcLineClient:
    def __init__(self, process: subprocess.Popen[str], *, timeout_seconds: float):
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("MCP process pipes were not created.")
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.timeout_seconds = timeout_seconds
        self.next_id = 1
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response = self.read_response(request_id)
        if "error" in response:
            raise RuntimeError(f"MCP {method} failed: {response['error']}")
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def write(self, payload: dict[str, Any]) -> None:
        self.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.stdin.flush()

    def read_response(self, request_id: int) -> dict[str, Any]:
        deadline = timeout_deadline(self.timeout_seconds)
        while True:
            remaining = max(0.01, deadline - monotonic_time())
            if remaining <= 0.01 and timeout_expired(deadline):
                raise TimeoutError(f"Timed out waiting for MCP response id {request_id}.")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for MCP response id {request_id}.") from exc
            if line is None:
                raise RuntimeError(f"MCP server exited before responding to id {request_id}.")
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == request_id:
                return payload

    def _read_stdout(self) -> None:
        for line in self.stdout:
            self.lines.put(line)
        self.lines.put(None)


def normalize_discovered_tools(tools: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    source = tools if isinstance(tools, list) else []
    for item in source:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        description = str(item.get("description", "")).strip()
        result.append({"name": name, "description": description})
    return result


def split_command(command: str) -> list[str]:
    return shlex.split(command)


def mcp_command_args(server: dict[str, Any]) -> list[str]:
    command = str(server.get("command", "")).strip()
    args = server.get("args")
    if isinstance(args, list) and args:
        return [*split_command(command), *[str(item) for item in args]]
    return split_command(command)


def _collect_stderr(process: subprocess.Popen[str], lines: list[str]) -> None:
    if process.stderr is None:
        return
    for line in process.stderr:
        lines.append(line.rstrip())


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            stream.close()


def timeout_deadline(timeout_seconds: float) -> float:
    import time

    return time.monotonic() + max(0.1, timeout_seconds)


def timeout_expired(deadline: float) -> bool:
    return monotonic_time() >= deadline


def monotonic_time() -> float:
    import time

    return time.monotonic()


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
        if server.get("args"):
            item["args_count"] = len(server.get("args", []))
        if server.get("env_keys"):
            item["env_keys"] = server.get("env_keys", [])
        if server.get("env_required"):
            item["env_required"] = True
        if server.get("tool_approvals"):
            item["tool_approvals"] = server.get("tool_approvals", {})
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


def import_codex_mcp_servers(
    workspace: Workspace,
    *,
    config_path: Path | None = None,
    names: list[str] | None = None,
    include_env: bool = False,
    enabled: bool = True,
) -> dict[str, Any]:
    source = config_path or Path.home() / ".codex" / "config.toml"
    data = tomllib.loads(source.read_text(encoding="utf-8-sig"))
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {"source": str(source), "imported": [], "skipped": [], "count": 0}
    selected_names = {name.strip() for name in (names or []) if name.strip()}
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for name, server in servers.items():
        server_name = str(name)
        if selected_names and server_name not in selected_names:
            continue
        if not isinstance(server, dict):
            skipped.append({"name": server_name, "reason": "invalid server config"})
            continue
        if not is_valid_mcp_name(server_name):
            skipped.append({"name": server_name, "reason": "invalid server name"})
            continue
        command = str(server.get("command", "")).strip()
        if not command:
            skipped.append({"name": server_name, "reason": "missing command"})
            continue
        env_keys = sorted(codex_server_env(server).keys())
        env = codex_server_env(server) if include_env else {}
        server_enabled = enabled and (include_env or not env_keys)
        imported_server = add_mcp_server(
            workspace,
            server_name,
            command=command,
            args=codex_server_args(server),
            cwd=str(server.get("cwd", "") or ""),
            env=env,
            env_keys=env_keys,
            env_required=bool(env_keys),
            startup_timeout_ms=safe_positive_int(server.get("startup_timeout_ms"), 10000),
            tool_approvals=codex_tool_approvals(server),
            enabled=server_enabled,
        )
        imported.append(redact_mcp_server(imported_server))
    return {
        "source": str(source),
        "include_env": include_env,
        "imported": imported,
        "skipped": skipped,
        "count": len(imported),
    }


def codex_server_args(server: dict[str, Any]) -> list[str]:
    args = server.get("args")
    if not isinstance(args, list):
        return []
    return [str(item) for item in args]


def codex_server_env(server: dict[str, Any]) -> dict[str, str]:
    env = server.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items() if str(key).strip()}


def codex_tool_approvals(server: dict[str, Any]) -> dict[str, dict[str, str]]:
    tools = server.get("tools")
    if not isinstance(tools, dict):
        return {}
    return normalize_tool_approvals(tools)


def effective_mcp_timeout(server: dict[str, Any], requested_seconds: float) -> float:
    startup_seconds = safe_positive_int(server.get("startup_timeout_ms"), 0) / 1000
    return max(float(requested_seconds), startup_seconds)


def safe_positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def redact_mcp_server(server: dict[str, Any]) -> dict[str, Any]:
    redacted = deepcopy(server)
    if redacted.get("env"):
        redacted["env"] = {key: "[set]" for key in redacted["env"].keys()}
    return redacted
