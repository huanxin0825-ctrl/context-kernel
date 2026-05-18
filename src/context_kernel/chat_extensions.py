from __future__ import annotations

import shlex
from typing import Any

from .cli_output import parse_json_object, print_json, print_mcp_call_result
from .marketplace import install_marketplace_skill, list_marketplace_skills
from .mcp import call_mcp_tool, list_mcp_servers, refresh_mcp_server_tools, set_mcp_server_enabled
from .skills import SkillRegistry, inspect_skill
from .storage import Workspace
from .terminal import chat_color, chat_notice, chat_panel, chat_width, truncate_line
from .tools import ToolExecutor


def print_extensions_panel(workspace: Workspace) -> None:
    summary = extension_summary(workspace)
    chat_panel(
        "Extensions Deck",
        [
            ("skills", f"{summary['skills']} registered"),
            ("mcp", f"{summary['mcp_enabled']}/{summary['mcp_total']} enabled servers"),
            ("mcp_tools", f"{summary['mcp_tools']} discovered tools"),
            ("commands", "/skills recommend <task> | /mcp refresh <name> | /mcp call <server> <tool>"),
            ("manage", "akernel skill list | akernel mcp list"),
        ],
    )
    print_skills_panel(workspace, limit=6)
    print_mcp_panel(workspace, limit=6)


def print_skills_panel(workspace: Workspace, *, limit: int = 10) -> None:
    skills = safe_registered_skills(workspace)
    if not skills:
        chat_notice("Skills", "No skills registered yet. Use `akernel skill register <skill.json>` or `akernel skill market-list`.")
        return
    print("")
    print(chat_color("[ Skills ]", "cyan", bold=True))
    for skill in skills[:limit]:
        line = f"  {skill.id:<22} {skill.name} - {skill.summary}"
        print(truncate_line(line, chat_width()))
    if len(skills) > limit:
        print(chat_color(f"  ... +{len(skills) - limit} more", "dim"))


def print_mcp_panel(workspace: Workspace, *, limit: int = 10) -> None:
    servers = safe_mcp_servers(workspace)
    if not servers:
        chat_notice("MCP", "No MCP servers configured. Add one with `akernel mcp add <name> --command ...`, then run `akernel mcp refresh <name>`.")
        return
    print("")
    print(chat_color("[ MCP ]", "cyan", bold=True))
    for server in servers[:limit]:
        enabled = "enabled" if server.get("enabled", True) else "disabled"
        tools = server.get("tools", [])
        tool_names = ", ".join(str(tool.get("name", "")) for tool in tools[:4] if isinstance(tool, dict) and tool.get("name"))
        suffix = f" tools={len(tools)}"
        if tool_names:
            suffix += f" [{tool_names}]"
        elif server.get("enabled", True):
            suffix += " [run refresh]"
        print(truncate_line(f"  {server.get('name', ''):<18} {enabled:<8} root={server.get('command_root', '')}{suffix}", chat_width()))
    if len(servers) > limit:
        print(chat_color(f"  ... +{len(servers) - limit} more", "dim"))


def handle_chat_mcp_command(workspace: Workspace, request: str) -> None:
    try:
        tokens = shlex.split(request, posix=True)
    except ValueError as exc:
        chat_notice("MCP", f"Could not parse command: {exc}")
        return
    if len(tokens) <= 1:
        print_mcp_panel(workspace)
        print(chat_color("  actions: /mcp refresh <name> | /mcp call <server> <tool> --args \"{...}\" | /mcp enable <name> | /mcp disable <name>", "dim"))
        return
    command = tokens[1].casefold()
    if command in {"list", "ls"}:
        print_mcp_panel(workspace)
        return
    if command == "refresh":
        if len(tokens) < 3:
            chat_notice("MCP", "Usage: /mcp refresh <name>")
            return
        timeout = parse_option_float(tokens[3:], "--timeout", default=10.0)
        server = refresh_mcp_server_tools(workspace, tokens[2], timeout_seconds=timeout)
        print(f"refreshed MCP server: {server['name']}")
        print(f"tools: {len(server.get('tools', []))}")
        for tool in server.get("tools", [])[:12]:
            print(f"  {tool['name']}\t{tool.get('description', '')}")
        return
    if command in {"enable", "disable"}:
        if len(tokens) < 3:
            chat_notice("MCP", f"Usage: /mcp {command} <name>")
            return
        server = set_mcp_server_enabled(workspace, tokens[2], command == "enable")
        state = "enabled" if server.get("enabled", True) else "disabled"
        print(f"{state} MCP server: {server['name']}")
        return
    if command == "call":
        if len(tokens) < 4:
            chat_notice("MCP", "Usage: /mcp call <server> <tool> --args \"{...}\"")
            return
        args_text = parse_option_value(tokens[4:], "--args", default="{}")
        timeout = parse_option_float(tokens[4:], "--timeout", default=10.0)
        allow_unknown = "--allow-unknown" in [token.casefold() for token in tokens[4:]]
        arguments = parse_json_object(strip_wrapping_quotes(args_text), label="MCP tool arguments")
        call = call_mcp_tool(
            workspace,
            tokens[2],
            tokens[3],
            arguments,
            timeout_seconds=timeout,
            allow_unknown=allow_unknown,
        )
        trace = ToolExecutor(workspace).record_external_tool(
            "mcp_call",
            subject=f"{tokens[2]}.{tokens[3]}",
            output=call,
            ok=True,
        )
        print(f"mcp call: {tokens[2]}.{tokens[3]}")
        print(f"trace: {trace['id']}")
        print_mcp_call_result(call["result"])
        return
    chat_notice("MCP", f"Unknown MCP chat command: {command}. Try /mcp, /mcp refresh <name>, or /mcp call <server> <tool>.")


def handle_chat_skills_command(workspace: Workspace, request: str) -> None:
    try:
        tokens = shlex.split(request, posix=True)
    except ValueError as exc:
        chat_notice("Skills", f"Could not parse command: {exc}")
        return
    if len(tokens) <= 1 or tokens[1].casefold() in {"list", "ls"}:
        print_skills_panel(workspace)
        print(chat_color("  actions: /skills show <id> | /skills inspect <id> | /skills recommend <task> | /skills install <id>", "dim"))
        return
    command = tokens[1].casefold()
    registry = SkillRegistry(workspace)
    if command == "show":
        if len(tokens) < 3:
            chat_notice("Skills", "Usage: /skills show <id> [--level l0|l1|l2|l3]")
            return
        level = parse_option_value(tokens[3:], "--level", default="l1")
        print_json(registry.get(tokens[2]).render_level(level))
        return
    if command == "inspect":
        if len(tokens) < 3:
            chat_notice("Skills", "Usage: /skills inspect <id> [--budget 300]")
            return
        budget = int(parse_option_float(tokens[3:], "--budget", default=300))
        print_json(inspect_skill(registry.get(tokens[2]), budget))
        return
    if command == "recommend":
        query = request.partition("recommend")[2].strip()
        if not query:
            chat_notice("Skills", "Usage: /skills recommend <task>")
            return
        selected = registry.select(query, budget_tokens=900, limit=5)
        if not selected:
            print("no matching skills")
            return
        print("recommended skills:")
        for item in selected:
            print(f"  {item.skill.id}\tlevel={item.level}\tscore={item.score}\t{item.reason}")
        return
    if command in {"market-list", "market"}:
        skills = list_marketplace_skills()
        if not skills:
            print("no marketplace skills")
            return
        print("marketplace skills:")
        for skill in skills[:12]:
            print(f"  {skill.get('id')}\t{skill.get('name')}\t{skill.get('summary', '')}")
        return
    if command in {"install", "market-install"}:
        if len(tokens) < 3:
            chat_notice("Skills", "Usage: /skills install <marketplace-skill-id>")
            return
        result = install_marketplace_skill(workspace, tokens[2])
        print(f"installed marketplace skill: {result['id']} ({result['name']})")
        print(f"version: {result.get('version')}")
        print(f"compatibility: {'ok' if result.get('compatibility', {}).get('ok') else 'warning'}")
        return
    chat_notice("Skills", f"Unknown skills chat command: {command}. Try /skills, /skills recommend <task>, or /skills install <id>.")


def parse_option_value(tokens: list[str], name: str, *, default: str) -> str:
    for index, token in enumerate(tokens):
        if token == name and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return default


def parse_option_float(tokens: list[str], name: str, *, default: float) -> float:
    value = parse_option_value(tokens, name, default=str(default))
    try:
        return float(strip_wrapping_quotes(value))
    except ValueError:
        return default


def strip_wrapping_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def extension_summary(workspace: Workspace) -> dict[str, int]:
    servers = safe_mcp_servers(workspace)
    return {
        "skills": len(safe_registered_skills(workspace)),
        "mcp_total": len(servers),
        "mcp_enabled": sum(1 for server in servers if server.get("enabled", True)),
        "mcp_tools": sum(len(server.get("tools", [])) for server in servers if server.get("enabled", True)),
    }


def safe_registered_skills(workspace: Workspace) -> list[Any]:
    try:
        return SkillRegistry(workspace).all()
    except Exception:
        return []


def safe_mcp_servers(workspace: Workspace) -> list[dict[str, Any]]:
    try:
        return list_mcp_servers(workspace, include_disabled=True)
    except Exception:
        return []

