from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from getpass import getpass
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any

from .agent_reports import build_agent_cost_report, load_agent_report, render_agent_cost_report
from .benchmarks import BenchmarkRunner, benchmark_ref, render_benchmark_evidence_markdown
from .budget import DEFAULT_PROFILE, profile_names
from .chat_commands import (
    attach_chat_file_command,
    attach_inline_file_references,
    expand_custom_chat_command,
    find_workspace_files,
    load_custom_chat_commands,
    print_custom_commands_panel,
    run_chat_command,
)
from .chat_extensions import (
    extension_summary,
    handle_chat_mcp_command,
    handle_chat_skills_command,
    print_extensions_panel,
    print_mcp_panel,
    print_skills_panel,
)
from .chat_tui import (
    build_chat_tui_screen,
    render_chat_tui_message,
    render_chat_tui_screen,
    render_chat_tui_status,
    render_chat_tui_update,
    tui_prompt,
)
from .chat_ui import (
    chat_help_groups,
    format_chat_help_text,
    format_tui_report,
    tui_flow_summary,
)
from .cli_output import parse_json_object, print_json, print_mcp_call_result
from .cli_reports import (
    aux_review_summary,
    benchmark_report_ok,
    enforce_benchmark_report_gate,
    enforce_regression_gate,
    list_agent_reports,
    print_agent_report,
    print_benchmark_check_summary,
    print_benchmark_diff,
    print_benchmark_report,
    print_policy_result,
)
from .cli_tools import add_tool_subcommands, load_batch_patch_specs
from .context import ContextBuilder
from .evals import EvalRunner
from .global_memory import pull_global_memories, push_global_memories
from .loop import AgentLoop
from .marketplace import install_marketplace_skill, is_remote_reference, list_marketplace_skills
from .mcp import (
    add_mcp_server,
    call_mcp_tool,
    get_mcp_server,
    import_codex_mcp_servers,
    list_mcp_servers,
    redact_mcp_server,
    refresh_mcp_server_tools,
    remove_mcp_server,
    set_mcp_server_enabled,
)
from .memory import ALLOWED_KINDS, MemoryStore
from .model_config import DEFAULT_AUXILIARY_MODEL, DEFAULT_PRIMARY_MODEL, auxiliary_model, primary_model
from .planner import ExecutionPlanner
from .policy import FILE_OPERATIONS, check_command_policy, check_file_policy, summarize_command_policy
from .project import load_project_profile, scan_project
from .providers import env_value, list_provider_models, normalize_openai_base_url
from .report_costs import build_benchmark_cost_report, build_eval_cost_report, render_cost_report
from .runner import AgentRunner
from .skills import (
    SkillRegistry,
    compile_markdown_skill,
    compile_markdown_skill_with_provider,
    inspect_skill,
    validate_skill_file,
)
from .state_writer import StateWriter
from .storage import Workspace
from .tasks import MILESTONE_STATUSES, TASK_STATUSES, TaskStore
from .terminal import (
    ascii_meter,
    chat_color,
    chat_notice,
    chat_panel,
    chat_rule,
    chat_width,
    compact_path,
    display_width,
    truncate_line,
    wrap_chat_text,
)
from .tools import ToolExecutor
from .verifier import verify_trace
from .workspace_summary import workspace_state_summary


CHAT_COMMANDS: list[tuple[str, str]] = [
    ("/help", "show command palette"),
    ("/status", "show workspace and runtime status"),
    ("/model", "show primary and auxiliary model roles"),
    ("/config", "show setup and environment guidance"),
    ("/extensions", "show MCP servers and registered skills"),
    ("/mcp", "show MCP server/tool availability"),
    ("/skills", "show registered skills"),
    ("/compact", "show compact task brief"),
    ("/commands", "list project and user slash commands"),
    ("/paste", "enter a multi-line task"),
    ("/task", "print current task session JSON"),
    ("/runs", "list recent agent runs"),
    ("/cost", "print last run cost report"),
    ("/up", "show older transcript lines"),
    ("/down", "move back toward latest messages"),
    ("/latest", "jump to latest messages"),
    ("/clear", "clear transcript"),
    ("/exit", "leave interactive session"),
]
OPENAI_ENV_KEYS = {
    "api_key": "AKERNEL_OPENAI_API_KEY",
    "base_url": "AKERNEL_OPENAI_BASE_URL",
    "model": "AKERNEL_OPENAI_MODEL",
    "aux_model": "AKERNEL_OPENAI_AUX_MODEL",
    "timeout_seconds": "AKERNEL_OPENAI_TIMEOUT_SECONDS",
    "max_retries": "AKERNEL_OPENAI_MAX_RETRIES",
    "retry_backoff_seconds": "AKERNEL_OPENAI_RETRY_BACKOFF_SECONDS",
}
LEGACY_OPENAI_ENV_KEYS = {
    "api_key": "CONTEXT_KERNEL_OPENAI_API_KEY",
    "base_url": "CONTEXT_KERNEL_OPENAI_BASE_URL",
    "model": "CONTEXT_KERNEL_OPENAI_MODEL",
    "aux_model": "CONTEXT_KERNEL_OPENAI_AUX_MODEL",
    "timeout_seconds": "CONTEXT_KERNEL_OPENAI_TIMEOUT_SECONDS",
    "max_retries": "CONTEXT_KERNEL_OPENAI_MAX_RETRIES",
    "retry_backoff_seconds": "CONTEXT_KERNEL_OPENAI_RETRY_BACKOFF_SECONDS",
}
COMMAND_NAMES = {
    "agent",
    "bench",
    "chat",
    "compare",
    "context",
    "doctor",
    "eval",
    "init",
    "memory",
    "mcp",
    "models",
    "plan",
    "policy",
    "project",
    "run",
    "setup",
    "skill",
    "task",
    "tool",
    "trace",
}


def main(argv: list[str] | None = None) -> None:
    configure_console_output()
    parser = build_parser()
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    raw_argv = normalize_default_chat_args(raw_argv)
    args = parser.parse_args(raw_argv)
    try:
        args.func(args)
    except Exception as exc:
        raise SystemExit(f"error: {exc}") from exc


def normalize_default_chat_args(raw_argv: list[str]) -> list[str]:
    """Let bare `akernel` accept chat flags without spelling out `chat`."""
    if not raw_argv:
        return ["chat"]
    if "-h" in raw_argv or "--help" in raw_argv:
        return raw_argv

    index = 0
    while index < len(raw_argv):
        token = raw_argv[index]
        if token == "--workspace":
            if index + 1 >= len(raw_argv):
                return raw_argv
            index += 2
            continue
        if token.startswith("--workspace="):
            index += 1
            continue
        break

    if index >= len(raw_argv):
        return raw_argv[:index] + ["chat"]
    if raw_argv[index] in COMMAND_NAMES:
        return raw_argv
    if raw_argv[index].startswith("-"):
        return raw_argv[:index] + ["chat"] + raw_argv[index:]
    return raw_argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akernel", description="Context Kernel CLI")
    parser.add_argument("--workspace", default=".", help="Workspace root containing .akernel state.")
    parser.set_defaults(
        func=cmd_chat,
        provider="openai",
        model=None,
        aux_model=None,
        model_routing="auto",
        aux_review="auto",
        base_url=None,
        budget=None,
        profile=DEFAULT_PROFILE,
        task=None,
        title="Interactive chat",
        max_steps=5,
        no_remember=False,
        allow_over_budget=False,
        expect_json=False,
        ui="auto",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Initialize a Context Kernel workspace.")
    init_parser.add_argument("path", nargs="?", default=".")
    init_parser.add_argument("--scan", action="store_true", help="Scan the project and save .akernel/project.json.")
    init_parser.add_argument("--no-config-update", action="store_true", help="Do not extend safe command roots from scan results.")
    init_parser.set_defaults(func=cmd_init)

    setup_parser = subparsers.add_parser("setup", help="Configure project-local provider environment.")
    setup_parser.add_argument("--api-key", default=None, help="OpenAI-compatible API key. If omitted, prompt securely.")
    setup_parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL. `/v1` is added when missing.")
    setup_parser.add_argument("--model", default=None, help="Default model id, for example gpt-5.5.")
    setup_parser.add_argument("--aux-model", default=None, help="Auxiliary model id for planning, review, and compression.")
    setup_parser.add_argument("--timeout-seconds", type=int, default=None, help="OpenAI-compatible request timeout. Defaults to 180.")
    setup_parser.add_argument("--max-retries", type=int, default=None, help="Network retries after the first attempt. Defaults to 3.")
    setup_parser.add_argument("--retry-backoff-seconds", type=float, default=None, help="Initial retry backoff in seconds. Defaults to 1.5.")
    setup_parser.add_argument("--env-file", default=None, help="Environment file path. Defaults to .env in the current project.")
    setup_parser.add_argument("--force", action="store_true", help="Rewrite an existing env file without keeping old values.")
    setup_parser.add_argument("--verify", action="store_true", help="List provider models after writing configuration.")
    setup_parser.set_defaults(func=cmd_setup)

    skill_parser = subparsers.add_parser("skill", help="Manage skill contracts.")
    skill_sub = skill_parser.add_subparsers(dest="skill_command", required=True)
    skill_register = skill_sub.add_parser("register", help="Register a skill JSON file.")
    skill_register.add_argument("json_file")
    skill_register.set_defaults(func=cmd_skill_register)
    skill_list = skill_sub.add_parser("list", help="List registered skills.")
    skill_list.set_defaults(func=cmd_skill_list)
    skill_show = skill_sub.add_parser("show", help="Show a registered skill.")
    skill_show.add_argument("skill_id")
    skill_show.add_argument("--level", choices=["l0", "l1", "l2", "l3"], default="l1")
    skill_show.set_defaults(func=cmd_skill_show)
    skill_compile = skill_sub.add_parser("compile", help="Compile a Markdown skill into structured JSON.")
    skill_compile.add_argument("markdown_file")
    skill_compile.add_argument("--id", default=None, help="Override compiled skill id.")
    skill_compile.add_argument("--output", default=None, help="Output JSON path. Defaults next to source.")
    skill_compile.add_argument("--provider", choices=["local", "openai"], default="local")
    skill_compile.add_argument("--model", default=None, help="Provider model id used when --provider is openai.")
    skill_compile.add_argument("--base-url", default=None, help="OpenAI-compatible base URL override.")
    skill_compile.add_argument("--register", action="store_true", help="Register the compiled skill in the current workspace.")
    skill_compile.set_defaults(func=cmd_skill_compile)
    skill_validate = skill_sub.add_parser("validate", help="Validate a skill JSON file.")
    skill_validate.add_argument("json_file")
    skill_validate.set_defaults(func=cmd_skill_validate)
    skill_inspect = skill_sub.add_parser("inspect", help="Inspect skill load levels and token estimates.")
    skill_inspect.add_argument("skill_id")
    skill_inspect.add_argument("--budget", type=int, default=300)
    skill_inspect.set_defaults(func=cmd_skill_inspect)
    skill_market_list = skill_sub.add_parser("market-list", help="List skills from a marketplace index.")
    skill_market_list.add_argument("--index", default=None, help="Marketplace index JSON path, file URL, or HTTP(S) URL.")
    skill_market_list.add_argument("--json", action="store_true")
    skill_market_list.set_defaults(func=cmd_skill_market_list)
    skill_market_install = skill_sub.add_parser("market-install", help="Install a skill from a marketplace index.")
    skill_market_install.add_argument("skill_id")
    skill_market_install.add_argument("--index", default=None, help="Marketplace index JSON path, file URL, or HTTP(S) URL.")
    skill_market_install.add_argument("--trust-remote", action="store_true", help="Allow installing a skill fetched from a remote marketplace source.")
    skill_market_install.add_argument("--ignore-compat", action="store_true", help="Install even when marketplace compatibility metadata does not match.")
    skill_market_install.add_argument("--json", action="store_true")
    skill_market_install.set_defaults(func=cmd_skill_market_install)

    mcp_parser = subparsers.add_parser("mcp", help="Manage MCP server configurations.")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command", required=True)
    mcp_add = mcp_sub.add_parser("add", help="Add or update a stdio MCP server.")
    mcp_add.add_argument("name")
    mcp_add.add_argument("--command", required=True, help="Command used to launch the stdio MCP server.")
    mcp_add.add_argument("--arg", action="append", default=[], help="Argument passed after --command. Repeatable.")
    mcp_add.add_argument("--cwd", default="", help="Optional working directory for the server command.")
    mcp_add.add_argument("--env", action="append", default=[], help="Environment variable as KEY=VALUE. Repeatable.")
    mcp_add.add_argument("--startup-timeout-ms", type=int, default=None, help="Startup timeout inherited by refresh/call.")
    mcp_add.add_argument("--tool", action="append", default=None, help="Tool summary as name:description. Repeatable.")
    mcp_add.add_argument("--disabled", action="store_true", help="Add the server but keep it disabled.")
    mcp_add.add_argument("--json", action="store_true")
    mcp_add.set_defaults(func=cmd_mcp_add)
    mcp_import_codex = mcp_sub.add_parser("import-codex", help="Import stdio MCP servers from a Codex config.toml.")
    mcp_import_codex.add_argument("--config", default=None, help="Codex config.toml path. Defaults to ~/.codex/config.toml.")
    mcp_import_codex.add_argument("--name", action="append", default=[], help="Only import this server name. Repeatable.")
    mcp_import_codex.add_argument("--include-env", action="store_true", help="Also copy env values. Defaults off to avoid writing secrets into the workspace.")
    mcp_import_codex.add_argument("--disabled", action="store_true", help="Import servers but keep them disabled.")
    mcp_import_codex.add_argument("--json", action="store_true")
    mcp_import_codex.set_defaults(func=cmd_mcp_import_codex)
    mcp_list = mcp_sub.add_parser("list", help="List configured MCP servers.")
    mcp_list.add_argument("--enabled-only", action="store_true")
    mcp_list.add_argument("--json", action="store_true")
    mcp_list.set_defaults(func=cmd_mcp_list)
    mcp_show = mcp_sub.add_parser("show", help="Show one MCP server configuration.")
    mcp_show.add_argument("name")
    mcp_show.set_defaults(func=cmd_mcp_show)
    mcp_refresh = mcp_sub.add_parser("refresh", help="Start a stdio MCP server and refresh tools/list summaries.")
    mcp_refresh.add_argument("name")
    mcp_refresh.add_argument("--timeout", type=float, default=10.0, help="Discovery timeout in seconds.")
    mcp_refresh.add_argument("--json", action="store_true")
    mcp_refresh.set_defaults(func=cmd_mcp_refresh)
    mcp_call = mcp_sub.add_parser("call", help="Call a discovered MCP tool manually and write a tool trace.")
    mcp_call.add_argument("name", help="MCP server name.")
    mcp_call.add_argument("tool", help="Tool name to call.")
    mcp_call.add_argument("--args", default="{}", help="Tool arguments as a JSON object.")
    mcp_call.add_argument("--timeout", type=float, default=10.0, help="Call timeout in seconds.")
    mcp_call.add_argument("--allow-unknown", action="store_true", help="Allow calling a tool not present in the discovered summary.")
    mcp_call.add_argument("--json", action="store_true")
    mcp_call.set_defaults(func=cmd_mcp_call)
    mcp_enable = mcp_sub.add_parser("enable", help="Enable an MCP server.")
    mcp_enable.add_argument("name")
    mcp_enable.set_defaults(func=cmd_mcp_enable)
    mcp_disable = mcp_sub.add_parser("disable", help="Disable an MCP server.")
    mcp_disable.add_argument("name")
    mcp_disable.set_defaults(func=cmd_mcp_disable)
    mcp_remove = mcp_sub.add_parser("remove", help="Remove an MCP server.")
    mcp_remove.add_argument("name")
    mcp_remove.set_defaults(func=cmd_mcp_remove)

    memory_parser = subparsers.add_parser("memory", help="Manage structured memory.")
    memory_sub = memory_parser.add_subparsers(dest="memory_command", required=True)
    memory_add = memory_sub.add_parser("add", help="Add a memory record.")
    memory_add.add_argument("--kind", required=True, choices=sorted(ALLOWED_KINDS))
    memory_add.add_argument("--text", required=True)
    memory_add.add_argument("--tags", default="", help="Comma-separated tags.")
    memory_add.set_defaults(func=cmd_memory_add)
    memory_show = memory_sub.add_parser("show", help="Show one memory record.")
    memory_show.add_argument("record_id")
    memory_show.add_argument("--include-archived", action="store_true")
    memory_show.set_defaults(func=cmd_memory_show)
    memory_list = memory_sub.add_parser("list", help="List memory records.")
    memory_list.add_argument("--kind", choices=sorted(ALLOWED_KINDS))
    memory_list.add_argument("--all", action="store_true", help="Include archived records.")
    memory_list.set_defaults(func=cmd_memory_list)
    memory_search = memory_sub.add_parser("search", help="Search memory records.")
    memory_search.add_argument("query")
    memory_search.add_argument("--kind", choices=sorted(ALLOWED_KINDS))
    memory_search.add_argument("--limit", type=int, default=5)
    memory_search.set_defaults(func=cmd_memory_search)
    memory_audit = memory_sub.add_parser("audit", help="Explain memory retention scores without archiving.")
    memory_audit.add_argument("--json", action="store_true")
    memory_audit.add_argument("--limit", type=int, default=20)
    memory_audit.set_defaults(func=cmd_memory_audit)
    memory_update = memory_sub.add_parser("update", help="Update an active memory record.")
    memory_update.add_argument("record_id")
    memory_update.add_argument("--kind", choices=sorted(ALLOWED_KINDS))
    memory_update.add_argument("--text")
    memory_update.add_argument("--tags", default=None, help="Comma-separated tags. Pass an empty value to clear.")
    memory_update.set_defaults(func=cmd_memory_update)
    memory_forget = memory_sub.add_parser("forget", help="Archive a memory record so it no longer enters context.")
    memory_forget.add_argument("record_id")
    memory_forget.set_defaults(func=cmd_memory_forget)
    memory_prune = memory_sub.add_parser("prune", help="Archive lower-priority memory records by count or token budget.")
    memory_prune.add_argument("--max-records", type=int, default=None)
    memory_prune.add_argument("--max-tokens", type=int, default=None)
    memory_prune.add_argument("--dry-run", action="store_true")
    memory_prune.add_argument("--json", action="store_true")
    memory_prune.set_defaults(func=cmd_memory_prune)
    memory_global_push = memory_sub.add_parser("global-push", help="Copy active project memories into the global memory store.")
    memory_global_push.add_argument("--kind", choices=sorted(ALLOWED_KINDS))
    memory_global_push.add_argument("--namespace", default=None, help="Global memory namespace. Defaults to the project directory name.")
    memory_global_push.add_argument("--tag", default=None, help="Only push memories containing this tag.")
    memory_global_push.add_argument("--dry-run", action="store_true", help="Preview records without copying them.")
    memory_global_push.add_argument("--global-root", default=None)
    memory_global_push.add_argument("--json", action="store_true")
    memory_global_push.set_defaults(func=cmd_memory_global_push)
    memory_global_pull = memory_sub.add_parser("global-pull", help="Copy memories from the global memory store into this project.")
    memory_global_pull.add_argument("--kind", choices=sorted(ALLOWED_KINDS))
    memory_global_pull.add_argument("--namespace", default=None, help="Only pull memories from this namespace.")
    memory_global_pull.add_argument("--source-project", default=None, help="Only pull memories pushed by this source project name.")
    memory_global_pull.add_argument("--tag", default=None, help="Only pull memories containing this tag.")
    memory_global_pull.add_argument("--limit", type=int, default=None)
    memory_global_pull.add_argument("--dry-run", action="store_true", help="Preview records without copying them.")
    memory_global_pull.add_argument("--global-root", default=None)
    memory_global_pull.add_argument("--json", action="store_true")
    memory_global_pull.set_defaults(func=cmd_memory_global_pull)

    run_parser = subparsers.add_parser("run", help="Build context and run a provider.")
    run_parser.add_argument("request")
    add_provider_args(run_parser)
    add_budget_args(run_parser)
    run_parser.add_argument("--show-packet", action="store_true")
    run_parser.add_argument("--allow-over-budget", action="store_true", help="Execute even when preflight budget verification fails.")
    run_parser.add_argument("--expect-json", action="store_true", help="Require the provider response to be valid JSON.")
    run_parser.add_argument("--remember", action="store_true", help="Write an explicit task-state memory from the run trace.")
    run_parser.add_argument("--task", default=None, help="Attach the run trace and written memories to a task session.")
    run_parser.add_argument("--resume", action="store_true", help="Inject the task brief into the context packet. Requires --task.")
    run_parser.set_defaults(func=cmd_run)

    agent_parser = subparsers.add_parser("agent", help="Run bounded agent loops.")
    agent_sub = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_run = agent_sub.add_parser("run", help="Run a bounded plan/run/verify/state loop.")
    agent_run.add_argument("request")
    add_provider_args(agent_run)
    add_budget_args(agent_run)
    agent_run.add_argument("--task", default=None, help="Continue an existing task; otherwise create a new one.")
    agent_run.add_argument("--max-steps", type=int, default=5, help="Maximum loop steps to run.")
    agent_run.add_argument("--model-routing", choices=["auto", "primary", "auxiliary"], default="primary", help="Choose how agent steps select primary vs auxiliary model. Use auto for cost-saving auxiliary first-step routing.")
    agent_run.add_argument("--aux-review", choices=["auto", "off", "always"], default="auto", help="Run auxiliary context review before selected agent steps.")
    agent_run.add_argument("--no-remember", action="store_true", help="Do not write explicit task-state memory.")
    agent_run.add_argument("--allow-over-budget", action="store_true", help="Execute even when preflight budget verification fails.")
    agent_run.add_argument("--expect-json", action="store_true", help="Require provider responses to be valid JSON.")
    agent_run.add_argument("--json", action="store_true")
    agent_run.set_defaults(func=cmd_agent_run)
    agent_list = agent_sub.add_parser("list", help="List saved agent loop reports.")
    agent_list.set_defaults(func=cmd_agent_list)
    agent_show = agent_sub.add_parser("show", help="Show a saved agent loop report.")
    agent_show.add_argument("run_id")
    agent_show.set_defaults(func=cmd_agent_show)
    agent_cost = agent_sub.add_parser("cost", help="Inspect token cost and pressure for a saved agent loop report.")
    agent_cost.add_argument("run_id")
    agent_cost.add_argument("--json", action="store_true")
    agent_cost.set_defaults(func=cmd_agent_cost)

    chat_parser = subparsers.add_parser("chat", help="Start an interactive agent workspace.")
    add_provider_args(chat_parser)
    add_budget_args(chat_parser)
    chat_parser.add_argument("--task", default=None, help="Continue an existing task session.")
    chat_parser.add_argument("--title", default="Interactive chat", help="Title for a new task session.")
    chat_parser.add_argument("--max-steps", type=int, default=5, help="Maximum agent loop steps per user message.")
    chat_parser.add_argument("--model-routing", choices=["auto", "primary", "auxiliary"], default="primary", help="Choose how agent steps select primary vs auxiliary model. Use auto for cost-saving auxiliary first-step routing.")
    chat_parser.add_argument("--aux-review", choices=["auto", "off", "always"], default="auto", help="Run auxiliary context review before selected agent steps.")
    chat_parser.add_argument("--no-remember", action="store_true", help="Do not write explicit task-state memory.")
    chat_parser.add_argument("--allow-over-budget", action="store_true", help="Execute even when preflight budget verification fails.")
    chat_parser.add_argument("--expect-json", action="store_true", help="Require provider responses to be valid JSON.")
    chat_parser.add_argument("--ui", choices=["auto", "classic", "tui"], default="auto", help="Choose interactive UI mode. auto uses TUI only on real terminals.")
    chat_parser.set_defaults(provider="openai", func=cmd_chat)

    models_parser = subparsers.add_parser("models", help="List models from a provider.")
    models_parser.add_argument("--provider", choices=["mock", "openai"], default="openai")
    models_parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL override.")
    models_parser.set_defaults(func=cmd_models)

    doctor_parser = subparsers.add_parser("doctor", help="Check local project configuration.")
    doctor_parser.set_defaults(func=cmd_doctor)

    project_parser = subparsers.add_parser("project", help="Scan and inspect project profile metadata.")
    project_sub = project_parser.add_subparsers(dest="project_command", required=True)
    project_scan = project_sub.add_parser("scan", help="Scan the workspace and write .akernel/project.json.")
    project_scan.add_argument("--no-config-update", action="store_true", help="Do not extend safe command roots from scan results.")
    project_scan.add_argument("--json", action="store_true", help="Print the full project profile JSON.")
    project_scan.set_defaults(func=cmd_project_scan)
    project_show = project_sub.add_parser("show", help="Show the saved project profile.")
    project_show.add_argument("--json", action="store_true", help="Print the full project profile JSON.")
    project_show.set_defaults(func=cmd_project_show)

    plan_parser = subparsers.add_parser("plan", help="Create an execution plan without calling a provider.")
    plan_parser.add_argument("request")
    add_budget_args(plan_parser)
    plan_parser.add_argument("--task", default=None, help="Use a task session for planning.")
    plan_parser.add_argument("--resume", action="store_true", help="Inject the task brief into the planned context. Requires --task.")
    plan_parser.add_argument("--json", action="store_true", help="Print the full plan JSON.")
    plan_parser.set_defaults(func=cmd_plan)

    policy_parser = subparsers.add_parser("policy", help="Check tool and file operation policy contracts.")
    policy_sub = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_file = policy_sub.add_parser("file", help="Check a planned file operation.")
    policy_file.add_argument("operation", choices=sorted(FILE_OPERATIONS))
    policy_file.add_argument("path")
    policy_file.add_argument("--allow-destructive", action="store_true")
    policy_file.add_argument("--json", action="store_true")
    policy_file.set_defaults(func=cmd_policy_file)
    policy_command = policy_sub.add_parser("command", help="Check a planned shell command without running it.")
    policy_command.add_argument("--allow-destructive", action="store_true")
    policy_command.add_argument("--json", action="store_true")
    policy_command.add_argument("command", nargs=argparse.REMAINDER)
    policy_command.set_defaults(func=cmd_policy_command)

    tool_parser = subparsers.add_parser("tool", help="Execute local tools through policy contracts.")
    tool_sub = tool_parser.add_subparsers(dest="tool_command", required=True)
    add_tool_subcommands(tool_sub)

    task_parser = subparsers.add_parser("task", help="Manage resumable task sessions.")
    task_sub = task_parser.add_subparsers(dest="task_command", required=True)
    task_start = task_sub.add_parser("start", help="Start a task session.")
    task_start.add_argument("title")
    task_start.add_argument("--goal", default=None)
    task_start.add_argument("--plan", action="store_true", help="Create a structured long-task plan immediately.")
    task_start.set_defaults(func=cmd_task_start)
    task_list = task_sub.add_parser("list", help="List task sessions.")
    task_list.add_argument("--status", choices=sorted(TASK_STATUSES))
    task_list.set_defaults(func=cmd_task_list)
    task_status = task_sub.add_parser("status", help="Show a task session.")
    task_status.add_argument("task_id")
    task_status.add_argument("--json", action="store_true")
    task_status.set_defaults(func=cmd_task_status)
    task_brief = task_sub.add_parser("brief", help="Build a compact resume brief for a task.")
    task_brief.add_argument("task_id")
    task_brief.add_argument("--json", action="store_true")
    task_brief.set_defaults(func=cmd_task_brief)
    task_plan = task_sub.add_parser("plan", help="Create or refresh a structured long-task plan.")
    task_plan.add_argument("task_id")
    task_plan.add_argument("--goal", default=None)
    task_plan.add_argument("--force", action="store_true", help="Replace an existing structured plan.")
    task_plan.add_argument("--json", action="store_true")
    task_plan.set_defaults(func=cmd_task_plan)
    task_next = task_sub.add_parser("next", help="Show the next resumable checkpoint for a task.")
    task_next.add_argument("task_id")
    task_next.add_argument("--json", action="store_true")
    task_next.set_defaults(func=cmd_task_next)
    task_checkpoint = task_sub.add_parser("checkpoint", help="Record long-task checkpoint progress.")
    task_checkpoint.add_argument("task_id")
    task_checkpoint.add_argument("--note", required=True)
    task_checkpoint.add_argument("--milestone", default=None)
    task_checkpoint.add_argument("--status", choices=sorted(MILESTONE_STATUSES), default=None)
    task_checkpoint.set_defaults(func=cmd_task_checkpoint)
    task_step = task_sub.add_parser("step", help="Append a checkpoint note.")
    task_step.add_argument("task_id")
    task_step.add_argument("--note", required=True)
    task_step.set_defaults(func=cmd_task_step)
    task_attach = task_sub.add_parser("attach", help="Attach a run/tool/memory reference.")
    task_attach.add_argument("task_id")
    task_attach.add_argument("kind", choices=["run", "tool", "memory"])
    task_attach.add_argument("ref_id")
    task_attach.set_defaults(func=cmd_task_attach)
    task_block = task_sub.add_parser("block", help="Mark a task as blocked.")
    task_block.add_argument("task_id")
    task_block.add_argument("--note", required=True)
    task_block.set_defaults(func=cmd_task_block)
    task_complete = task_sub.add_parser("complete", help="Mark a task as completed.")
    task_complete.add_argument("task_id")
    task_complete.add_argument("--note", default=None)
    task_complete.set_defaults(func=cmd_task_complete)

    context_parser = subparsers.add_parser("context", help="Inspect context assembly without provider execution.")
    context_parser.add_argument("request")
    add_budget_args(context_parser)
    context_parser.add_argument("--task", default=None, help="Use a task session for context assembly.")
    context_parser.add_argument("--resume", action="store_true", help="Inject the task brief into the context packet. Requires --task.")
    context_parser.set_defaults(func=cmd_context)

    compare_parser = subparsers.add_parser("compare", help="Compare minimal context against a full-load baseline.")
    compare_parser.add_argument("request")
    add_budget_args(compare_parser)
    compare_parser.add_argument("--json", action="store_true", help="Print the full comparison JSON.")
    compare_parser.set_defaults(func=cmd_compare)

    eval_parser = subparsers.add_parser("eval", help="Run comparison fixtures.")
    eval_sub = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_run = eval_sub.add_parser("run", help="Run eval fixture JSON.")
    eval_run.add_argument("fixture")
    add_budget_args(eval_run)
    add_eval_provider_args(eval_run)
    eval_run.add_argument("--json", action="store_true", help="Print the full eval report JSON.")
    eval_run.add_argument("--no-save", action="store_true", help="Do not persist the eval report.")
    eval_run.set_defaults(func=cmd_eval_run)
    eval_list = eval_sub.add_parser("list", help="List saved eval reports.")
    eval_list.set_defaults(func=cmd_eval_list)
    eval_show = eval_sub.add_parser("show", help="Show a saved eval report.")
    eval_show.add_argument("report_id")
    eval_show.set_defaults(func=cmd_eval_show)
    eval_cost = eval_sub.add_parser("cost", help="Inspect token cost hotspots for a saved eval report.")
    eval_cost.add_argument("report_id")
    eval_cost.add_argument("--json", action="store_true", help="Print the full eval cost JSON.")
    eval_cost.set_defaults(func=cmd_eval_cost)
    eval_diff = eval_sub.add_parser("diff", help="Compare two saved eval reports.")
    eval_diff.add_argument("before_id")
    eval_diff.add_argument("after_id")
    eval_diff.add_argument("--json", action="store_true", help="Print the full diff JSON.")
    eval_diff.add_argument("--fail-on-regression", action="store_true", help="Exit non-zero when regressions are detected.")
    eval_diff.set_defaults(func=cmd_eval_diff)

    bench_parser = subparsers.add_parser("bench", help="Run benchmark fixture directories.")
    bench_sub = bench_parser.add_subparsers(dest="bench_command", required=True)
    bench_run = bench_sub.add_parser("run", help="Run all eval fixtures in a directory.")
    bench_run.add_argument("directory")
    add_budget_args(bench_run)
    add_eval_provider_args(bench_run)
    bench_run.add_argument("--json", action="store_true", help="Print the full benchmark report JSON.")
    bench_run.add_argument("--no-save", action="store_true", help="Do not persist the benchmark report.")
    bench_run.set_defaults(func=cmd_bench_run)
    bench_gate = bench_sub.add_parser("gate", help="Run a benchmark and fail if it regresses against a saved baseline.")
    bench_gate.add_argument("directory")
    add_budget_args(bench_gate)
    add_eval_provider_args(bench_gate)
    bench_gate.add_argument("--baseline-report", default=None, help="Saved benchmark report id to compare against.")
    bench_gate.add_argument("--require-baseline", action="store_true", help="Exit non-zero when no matching baseline report exists.")
    bench_gate.add_argument("--json", action="store_true", help="Print the full benchmark gate JSON.")
    bench_gate.set_defaults(func=cmd_bench_gate)
    bench_list = bench_sub.add_parser("list", help="List saved benchmark reports.")
    bench_list.set_defaults(func=cmd_bench_list)
    bench_show = bench_sub.add_parser("show", help="Show a saved benchmark report.")
    bench_show.add_argument("report_id")
    bench_show.set_defaults(func=cmd_bench_show)
    bench_cost = bench_sub.add_parser("cost", help="Inspect token cost hotspots for a saved benchmark report.")
    bench_cost.add_argument("report_id")
    bench_cost.add_argument("--json", action="store_true", help="Print the full benchmark cost JSON.")
    bench_cost.set_defaults(func=cmd_bench_cost)
    bench_diff = bench_sub.add_parser("diff", help="Compare two saved benchmark reports.")
    bench_diff.add_argument("before_id")
    bench_diff.add_argument("after_id")
    bench_diff.add_argument("--json", action="store_true", help="Print the full benchmark diff JSON.")
    bench_diff.add_argument("--fail-on-regression", action="store_true", help="Exit non-zero when regressions are detected.")
    bench_diff.set_defaults(func=cmd_bench_diff)
    bench_export = bench_sub.add_parser("export", help="Export a benchmark report as Markdown.")
    bench_export.add_argument("report_id")
    bench_export.add_argument("--output", default=None, help="Output markdown path.")
    bench_export.set_defaults(func=cmd_bench_export)
    bench_evidence = bench_sub.add_parser("evidence", help="Summarize saved benchmark reports as token-savings evidence.")
    bench_evidence.add_argument("report_ids", nargs="*", help="Specific report ids. Defaults to recent saved reports.")
    bench_evidence.add_argument("--limit", type=int, default=None, help="Limit recent reports when no ids are provided.")
    bench_evidence.add_argument("--output", default=None, help="Write Markdown evidence to this path.")
    bench_evidence.add_argument("--json", action="store_true", help="Print the full evidence JSON.")
    bench_evidence.add_argument("--fail-under", type=float, default=None, help="Exit non-zero if total savings percent is below this threshold.")
    bench_evidence.set_defaults(func=cmd_bench_evidence)

    trace_parser = subparsers.add_parser("trace", help="Inspect run traces.")
    trace_sub = trace_parser.add_subparsers(dest="trace_command", required=True)
    trace_list = trace_sub.add_parser("list", help="List traces.")
    trace_list.set_defaults(func=cmd_trace_list)
    trace_show = trace_sub.add_parser("show", help="Show a trace.")
    trace_show.add_argument("trace_id")
    trace_show.set_defaults(func=cmd_trace_show)
    trace_verify = trace_sub.add_parser("verify", help="Re-run verifier checks on a saved trace.")
    trace_verify.add_argument("trace_id")
    trace_verify.add_argument("--expect-json", action="store_true")
    trace_verify.set_defaults(func=cmd_trace_verify)
    trace_remember = trace_sub.add_parser("remember", help="Write memory records from a saved trace.")
    trace_remember.add_argument("trace_id")
    trace_remember.add_argument("--dry-run", action="store_true", help="Show proposed memory records without writing them.")
    trace_remember.set_defaults(func=cmd_trace_remember)

    return parser


def configure_console_output() -> None:
    for stream in [sys.stdout, sys.stderr]:
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def add_budget_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--budget", type=int, default=None, help="Override the selected profile's token budget.")
    parser.add_argument("--profile", choices=profile_names(), default=DEFAULT_PROFILE, help="Budget profile.")


def add_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=["mock", "openai"], default="mock")
    parser.add_argument("--model", default=None, help="Provider model id. Defaults to gpt-5.5 for openai.")
    parser.add_argument("--aux-model", default=None, help="Auxiliary model id for planning, review, and compression.")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL override.")


def add_eval_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute", action="store_true", help="Execute each eval task with a provider.")
    parser.add_argument("--provider", choices=["mock", "openai"], default="mock", help="Provider used with --execute.")
    parser.add_argument("--model", default=None, help="Provider model id used with --execute.")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL override used with --execute.")


def workspace_from_args(args: argparse.Namespace, *, initialized: bool = True) -> Workspace:
    workspace = Workspace(Path(args.workspace))
    if initialized:
        workspace.require_initialized()
    return workspace


def chat_workspace_from_args(args: argparse.Namespace) -> Workspace:
    workspace = Workspace(Path(args.workspace))
    if not workspace.state.exists():
        workspace.init()
        print(f"initialized workspace: {workspace.state}")
    return workspace


def cmd_init(args: argparse.Namespace) -> None:
    workspace = Workspace(Path(args.path))
    workspace.init()
    print(f"initialized: {workspace.state}")
    if args.scan:
        profile = scan_project(workspace, update_config=not args.no_config_update)
        print_project_scan_summary(profile, config_updated=not args.no_config_update)


def cmd_project_scan(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    profile = scan_project(workspace, update_config=not args.no_config_update)
    if args.json:
        print_json(profile)
        return
    print_project_scan_summary(profile, config_updated=not args.no_config_update)


def cmd_project_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    profile = load_project_profile(workspace)
    if not profile:
        raise FileNotFoundError(f"No project profile found: {workspace.project_file}. Run `akernel project scan` first.")
    if args.json:
        print_json(profile)
        return
    print_project_scan_summary(profile, config_updated=False)


def cmd_setup(args: argparse.Namespace) -> None:
    env_path = Path(args.env_file) if args.env_file else Path.cwd() / ".env"
    existing = parse_simple_env(env_path) if env_path.exists() and not args.force else {}
    interactive = sys.stdin.isatty()

    api_key = args.api_key
    if api_key is None:
        current_key = existing_env_value(existing, "api_key")
        if current_key and interactive:
            typed = getpass("API key [keep existing]: ").strip()
            api_key = typed or current_key
        elif interactive:
            api_key = getpass("API key: ").strip()
        else:
            api_key = current_key
    if not api_key:
        raise ValueError("Missing API key. Run `akernel setup --api-key <key>` or use interactive setup.")

    base_url = args.base_url
    if base_url is None:
        default_base_url = existing_env_value(existing, "base_url") or "https://clarmy.cloud/v1"
        base_url = prompt_text("Base URL", default_base_url, interactive=interactive)
    base_url = normalize_openai_base_url(base_url)

    model = args.model
    if model is None:
        default_model = existing_env_value(existing, "model") or DEFAULT_PRIMARY_MODEL
        model = prompt_text("Primary model", default_model, interactive=interactive)

    aux_model = args.aux_model
    if aux_model is None:
        default_aux_model = existing_env_value(existing, "aux_model") or DEFAULT_AUXILIARY_MODEL
        aux_model = prompt_text("Auxiliary model", default_aux_model, interactive=interactive)

    timeout_seconds = str(args.timeout_seconds or existing_env_value(existing, "timeout_seconds") or "180")
    max_retries = str(args.max_retries if args.max_retries is not None else existing_env_value(existing, "max_retries") or "3")
    retry_backoff_seconds = str(
        args.retry_backoff_seconds
        if args.retry_backoff_seconds is not None
        else existing_env_value(existing, "retry_backoff_seconds") or "1.5"
    )

    write_project_env(
        env_path,
        api_key=api_key,
        base_url=base_url,
        model=model,
        aux_model=aux_model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    print(f"configured: {env_path}")
    print("api_key: set")
    print(f"base_url: {base_url}")
    print(f"primary_model: {model}")
    print(f"auxiliary_model: {aux_model}")
    print(f"timeout_seconds: {timeout_seconds}")
    print(f"max_retries: {max_retries}")
    print(f"retry_backoff_seconds: {retry_backoff_seconds}")
    if args.verify:
        previous = {key: os.environ.get(key) for key in OPENAI_ENV_KEYS.values()}
        try:
            os.environ[OPENAI_ENV_KEYS["api_key"]] = api_key
            os.environ[OPENAI_ENV_KEYS["base_url"]] = base_url
            os.environ[OPENAI_ENV_KEYS["model"]] = model
            os.environ[OPENAI_ENV_KEYS["aux_model"]] = aux_model
            models = list_provider_models("openai", base_url=base_url)
        finally:
            restore_env(previous)
        print("models:")
        for item in models[:20]:
            print(f"- {item}")


def parse_simple_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lstrip("\ufeff")] = value.strip().strip('"').strip("'")
    return values


def prompt_text(label: str, default: str, *, interactive: bool) -> str:
    if not interactive:
        return default
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def existing_env_value(existing: dict[str, str], key: str) -> str:
    return existing.get(OPENAI_ENV_KEYS[key]) or existing.get(LEGACY_OPENAI_ENV_KEYS[key]) or ""


def write_project_env(
    path: Path,
    *,
    api_key: str,
    base_url: str,
    model: str,
    aux_model: str,
    timeout_seconds: str = "180",
    max_retries: str = "3",
    retry_backoff_seconds: str = "1.5",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{OPENAI_ENV_KEYS['api_key']}={api_key}",
        f"{OPENAI_ENV_KEYS['base_url']}={base_url}",
        f"{OPENAI_ENV_KEYS['model']}={model}",
        f"{OPENAI_ENV_KEYS['aux_model']}={aux_model}",
        f"{OPENAI_ENV_KEYS['timeout_seconds']}={timeout_seconds}",
        f"{OPENAI_ENV_KEYS['max_retries']}={max_retries}",
        f"{OPENAI_ENV_KEYS['retry_backoff_seconds']}={retry_backoff_seconds}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def restore_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def parse_env_assignments(values: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for value in values:
        key, separator, raw = value.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError("MCP --env values must use KEY=VALUE.")
        env[key] = raw
    return env


def cmd_skill_register(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    skill = SkillRegistry(workspace).register(Path(args.json_file))
    print(f"registered skill: {skill.id} ({skill.name})")


def cmd_skill_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    skills = SkillRegistry(workspace).all()
    if not skills:
        print("no skills registered")
        return
    for skill in skills:
        print(f"{skill.id}\t{skill.name}\t{skill.summary}")


def cmd_skill_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    skill = SkillRegistry(workspace).get(args.skill_id)
    print_json(skill.render_level(args.level))


def cmd_skill_compile(args: argparse.Namespace) -> None:
    source = Path(args.markdown_file)
    metadata = None
    if args.provider == "local":
        skill = compile_markdown_skill(source, skill_id=args.id)
    else:
        skill, metadata = compile_markdown_skill_with_provider(
            source,
            provider_name=args.provider,
            model=args.model,
            base_url=args.base_url,
            skill_id=args.id,
        )
    output = Path(args.output) if args.output else source.with_suffix(".json")
    Workspace.write_json(output, skill.to_dict())
    print(f"compiled skill: {skill.id} -> {output}")
    if metadata:
        print(f"provider: {metadata['provider']} model={metadata['model']} tokens={metadata['total_tokens']}")
    if args.register:
        workspace = workspace_from_args(args)
        registered = SkillRegistry(workspace).register(output)
        print(f"registered skill: {registered.id} ({registered.name})")


def cmd_skill_validate(args: argparse.Namespace) -> None:
    print_json(validate_skill_file(Path(args.json_file)))


def cmd_skill_inspect(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    skill = SkillRegistry(workspace).get(args.skill_id)
    print_json(inspect_skill(skill, args.budget))


def cmd_skill_market_list(args: argparse.Namespace) -> None:
    index = args.index if args.index else None
    skills = list_marketplace_skills(index)
    if args.json:
        print_json({"count": len(skills), "skills": skills})
        return
    if not skills:
        print("no marketplace skills")
        return
    for skill in skills:
        compat = skill.get("compatibility_check", {})
        remote = "remote" if skill.get("remote") else "local"
        print(
            f"{skill.get('id')}\t{skill.get('name')}\t"
            f"v{skill.get('version', '0.0.0')}\t{remote}\t"
            f"compat={'ok' if compat.get('ok', True) else 'blocked'}\t"
            f"{skill.get('summary', '')}"
        )


def cmd_skill_market_install(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    index = args.index if args.index else None
    trust_remote = args.trust_remote
    if index and is_remote_reference(index) and not trust_remote and sys.stdin.isatty():
        answer = input(f"Install from remote marketplace {index}? Type 'yes' to trust this source: ").strip().casefold()
        trust_remote = answer == "yes"
    result = install_marketplace_skill(
        workspace,
        args.skill_id,
        index=index,
        trust_remote=trust_remote,
        ignore_compat=args.ignore_compat,
    )
    if args.json:
        print_json(result)
        return
    print(f"installed marketplace skill: {result['id']} ({result['name']})")
    print(f"version: {result.get('version')}")
    print(f"source: {result.get('source')}")
    print(f"compatibility: {'ok' if result.get('compatibility', {}).get('ok') else 'warning'}")


def cmd_mcp_add(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    server = add_mcp_server(
        workspace,
        args.name,
        command=args.command,
        args=args.arg,
        cwd=args.cwd,
        env=parse_env_assignments(args.env),
        startup_timeout_ms=args.startup_timeout_ms,
        tools=args.tool,
        enabled=not args.disabled,
    )
    if args.json:
        print_json(server)
        return
    state = "enabled" if server.get("enabled") else "disabled"
    print(f"mcp: {server['name']} ({state})")
    print(f"transport: {server['transport']}")
    print(f"command: {server['command']}")
    if server.get("args"):
        print(f"args: {len(server['args'])}")
    if server.get("env_keys"):
        print(f"env: {len(server['env_keys'])} key(s)")
    if server.get("tools"):
        print(f"tools: {len(server['tools'])}")


def cmd_mcp_import_codex(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    result = import_codex_mcp_servers(
        workspace,
        config_path=Path(args.config) if args.config else None,
        names=args.name,
        include_env=args.include_env,
        enabled=not args.disabled,
    )
    if args.json:
        print_json(result)
        return
    print(f"imported MCP servers from Codex: {result['count']}")
    for server in result.get("imported", []):
        env_note = f", env_keys={len(server.get('env_keys', []))}" if server.get("env_keys") else ""
        arg_note = f", args={len(server.get('args', []))}" if server.get("args") else ""
        print(f"  {server['name']} ({'enabled' if server.get('enabled') else 'disabled'}{arg_note}{env_note})")
    for skipped in result.get("skipped", []):
        print(f"  skipped {skipped['name']}: {skipped['reason']}")
    if not args.include_env:
        print("env values were not copied; rerun with --include-env only if this workspace may store those secrets.")


def cmd_mcp_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    servers = list_mcp_servers(workspace, include_disabled=not args.enabled_only)
    if args.json:
        print_json({"count": len(servers), "servers": servers})
        return
    if not servers:
        print("no MCP servers configured")
        return
    for server in servers:
        status = "enabled" if server.get("enabled") else "disabled"
        tools = len(server.get("tools", []))
        args_suffix = f" args={len(server.get('args', []))}" if server.get("args") else ""
        env_suffix = f" env_keys={len(server.get('env_keys', []))}" if server.get("env_keys") else ""
        print(f"{server['name']}\t{status}\t{server['transport']}\ttools={tools}{args_suffix}{env_suffix}\t{server['command']}")


def cmd_mcp_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    print_json(redact_mcp_server(get_mcp_server(workspace, args.name)))


def cmd_mcp_refresh(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    server = refresh_mcp_server_tools(workspace, args.name, timeout_seconds=args.timeout)
    if args.json:
        print_json(server)
        return
    discovery = server.get("discovery", {})
    print(f"refreshed MCP server: {server['name']}")
    print(f"tools: {discovery.get('tool_count', len(server.get('tools', [])))}")
    server_info = discovery.get("server_info") or {}
    if server_info:
        print(f"server: {server_info.get('name', '')} {server_info.get('version', '')}".strip())
    for tool in server.get("tools", []):
        print(f"  {tool['name']}\t{tool.get('description', '')}")


def cmd_mcp_call(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    arguments = parse_json_object(args.args, label="MCP tool arguments")
    call = call_mcp_tool(
        workspace,
        args.name,
        args.tool,
        arguments,
        timeout_seconds=args.timeout,
        allow_unknown=args.allow_unknown,
    )
    trace = ToolExecutor(workspace).record_external_tool(
        "mcp_call",
        subject=f"{args.name}.{args.tool}",
        output=call,
        ok=True,
    )
    if args.json:
        print_json(trace)
        return
    print(f"mcp call: {args.name}.{args.tool}")
    print(f"trace: {trace['id']}")
    print_mcp_call_result(call["result"])


def cmd_mcp_enable(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    server = set_mcp_server_enabled(workspace, args.name, True)
    print(f"enabled MCP server: {server['name']}")


def cmd_mcp_disable(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    server = set_mcp_server_enabled(workspace, args.name, False)
    print(f"disabled MCP server: {server['name']}")


def cmd_mcp_remove(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    server = remove_mcp_server(workspace, args.name)
    print(f"removed MCP server: {server['name']}")


def cmd_memory_add(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    record = MemoryStore(workspace).add(kind=args.kind, text=args.text, tags=tags)
    print(f"memory: {record.id} ({record.kind})")


def cmd_memory_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    record = MemoryStore(workspace).get(args.record_id, include_archived=args.include_archived)
    print_json(record.to_dict())


def cmd_memory_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    records = MemoryStore(workspace).all(kind=args.kind, include_archived=args.all)
    if not records:
        print("no memory records")
        return
    for record in records:
        status = "archived" if record.archived_at else "active"
        print(f"{record.id}\t{record.kind}\t{status}\t{record.text}")


def cmd_memory_search(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    results = MemoryStore(workspace).search(args.query, kind=args.kind, limit=args.limit)
    if not results:
        print("no matching memory")
        return
    for item in results:
        print(f"{item.record.id}\t{item.record.kind}\tscore={item.score}\t{item.record.text}")


def cmd_memory_audit(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    decisions = sorted(MemoryStore(workspace).retention_analysis(), key=lambda item: item["retention_key"], reverse=True)
    if args.json:
        print_json({"count": len(decisions), "decisions": [strip_cli_retention_key(item) for item in decisions]})
        return
    if not decisions:
        print("no memory records")
        return
    for decision in decisions[: args.limit]:
        record = decision["record"]
        recoverability = decision["recoverability"]
        print(
            f"{record['id']}\t{record['kind']}\tscore={decision['score']}\t"
            f"tokens={decision['token_cost']}\trecoverable={recoverability['level']}\t{record['text']}"
        )
        print(f"  reasons: {', '.join(decision['reasons'])}")


def cmd_memory_update(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    tags = None if args.tags is None else [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    record = MemoryStore(workspace).update(args.record_id, kind=args.kind, text=args.text, tags=tags)
    print(f"updated memory: {record.id} ({record.kind})")


def cmd_memory_forget(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    removed = MemoryStore(workspace).forget(args.record_id)
    if not removed:
        raise KeyError(f"Memory record not found: {args.record_id}")
    print(f"forgot memory: {args.record_id}")


def cmd_memory_prune(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    result = MemoryStore(workspace).prune(
        max_records=args.max_records,
        max_tokens=args.max_tokens,
        dry_run=args.dry_run,
    )
    if args.json:
        print_json(result)
        return
    action = "would archive" if args.dry_run else "archived"
    print(
        f"memory_prune: active_before={result['active_before']} "
        f"kept={result['kept']} {action}={result['candidate_count']} "
        f"kept_tokens={result['kept_tokens']}"
    )
    for decision in result.get("candidate_decisions", [])[:5]:
        record = decision["record"]
        recoverability = decision["recoverability"]
        print(
            f"- {record['id']} {record['kind']} score={decision['score']} "
            f"tokens={decision['token_cost']} recoverable={recoverability['level']}: {record['text']}"
        )
        print(f"  reasons: {', '.join(decision['reasons'])}")


def strip_cli_retention_key(decision: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in decision.items() if key != "retention_key"}


def cmd_memory_global_push(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    result = push_global_memories(
        workspace,
        kind=args.kind,
        namespace=args.namespace,
        tag=args.tag,
        dry_run=args.dry_run,
        global_root=Path(args.global_root) if args.global_root else None,
    )
    if args.json:
        print_json(result)
        return
    action = "would copy" if result.get("dry_run") else "copied"
    print(
        f"global_push: {action} {result['candidate_count']} memory record(s) "
        f"to {result['target']} namespace={result['namespace']}"
    )
    print_global_memory_preview(result)


def cmd_memory_global_pull(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    result = pull_global_memories(
        workspace,
        kind=args.kind,
        namespace=args.namespace,
        source_project=args.source_project,
        tag=args.tag,
        limit=args.limit,
        dry_run=args.dry_run,
        global_root=Path(args.global_root) if args.global_root else None,
    )
    if args.json:
        print_json(result)
        return
    action = "would copy" if result.get("dry_run") else "copied"
    print(f"global_pull: {action} {result['candidate_count']} memory record(s) from {result['source']}")
    print_global_memory_preview(result)


def print_global_memory_preview(result: dict[str, Any], *, limit: int = 5) -> None:
    records = result.get("records", [])
    if not isinstance(records, list) or not records:
        return
    for record in records[:limit]:
        if not isinstance(record, dict):
            continue
        tags = ", ".join(record.get("tags", [])[:6])
        print(f"- {record.get('id')} {record.get('kind')} tags=[{tags}] {record.get('text')}")
    if len(records) > limit:
        print(f"... {len(records) - limit} more")


def cmd_context(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_resume_args(args)
    packet = ContextBuilder(workspace).build(
        args.request,
        args.budget,
        args.profile,
        task_id=args.task,
        resume=args.resume,
    )
    print_json(packet)


def cmd_compare(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    comparison = ContextBuilder(workspace).compare(args.request, args.budget, args.profile)
    if args.json:
        print_json(comparison)
        return

    print(f"request: {comparison['request']}")
    print(f"profile: {comparison['profile']}")
    print(f"budget: {comparison['budget']}")
    print(f"kernel_tokens: {comparison['kernel']['estimated_tokens']}")
    print(f"baseline_tokens: {comparison['baseline']['estimated_tokens']}")
    print(f"savings: {comparison['savings']['estimated_tokens']} tokens ({comparison['savings']['percent']}%)")
    print(f"kernel_selected: memory={comparison['kernel']['selected_memory']} skills={comparison['kernel']['selected_skills']}")
    print(f"baseline_loaded: memory={comparison['baseline']['loaded_memory']} skills={comparison['baseline']['loaded_skills']} level={comparison['baseline']['skill_level']}")


def cmd_run(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_resume_args(args)
    ensure_task_attachable(workspace, args.task)
    trace = AgentRunner(workspace).run(
        args.request,
        provider_name=args.provider,
        budget=args.budget,
        profile=args.profile,
        model=args.model,
        base_url=args.base_url,
        allow_over_budget=args.allow_over_budget,
        expect_json=args.expect_json,
        remember=args.remember,
        task_id=args.task,
        resume=args.resume,
    )
    print(trace["response"]["text"])
    print("")
    print(f"trace: {trace['id']}")
    print(f"tokens: input={trace['response']['input_tokens']} output={trace['response']['output_tokens']} total={trace['response']['total_tokens']}")
    print(f"verifier: {'ok' if trace['verifier']['ok'] else 'failed'}")
    if trace.get("state", {}).get("enabled"):
        print(f"state: wrote {trace['state']['written_count']} memory record(s)")
    if args.task:
        attach_run_to_task(workspace, args.task, trace)
        print(f"task: attached run {trace['id']} to {args.task}")
    if args.show_packet:
        print_json(trace["context_packet"])


def cmd_agent_run(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    report = AgentLoop(workspace).run(
        args.request,
        provider_name=args.provider,
        budget=args.budget,
        profile=args.profile,
        model=args.model,
        aux_model=args.aux_model,
        model_routing=args.model_routing,
        aux_review=args.aux_review,
        base_url=args.base_url,
        task_id=args.task,
        max_steps=args.max_steps,
        remember=not args.no_remember,
        allow_over_budget=args.allow_over_budget,
        expect_json=args.expect_json,
        progress_callback=None if args.json else print_agent_progress_event,
    )
    if args.json:
        print_json(report)
        return

    print_agent_report(report)


def cmd_chat(args: argparse.Namespace) -> None:
    workspace = chat_workspace_from_args(args)
    tasks = TaskStore(workspace)
    if args.task:
        ensure_task_attachable(workspace, args.task)
        task_id = args.task
    else:
        task = tasks.start(args.title, goal="Interactive agent chat session")
        task_id = task["id"]

    last_report: dict[str, Any] | None = None
    state: dict[str, Any] = {"last_report": None, "file_matches": []}
    pending_context: list[str] = []
    if resolve_chat_ui(args) == "tui":
        run_chat_loop_tui(workspace, tasks, task_id, args)
        return

    print_chat_header(workspace, task_id, args)
    while True:
        try:
            request = read_chat_input(chat_prompt(args), workspace).strip()
        except EOFError:
            print("")
            break
        except KeyboardInterrupt:
            print("")
            print("interrupted")
            break
        if not request:
            continue
        lowered = request.lower()
        if lowered in {"/exit", "/quit", "exit", "quit"}:
            print("bye")
            break
        if lowered == "/help":
            print_chat_help()
            continue
        if lowered == "/compact":
            print_task_brief_panel(tasks, task_id)
            continue
        if lowered == "/commands":
            print_custom_commands_panel(workspace.root)
            continue
        if lowered in {"/extensions", "/ext"}:
            print_extensions_panel(workspace)
            continue
        if lowered.startswith("/mcp"):
            handle_chat_mcp_command(workspace, request)
            continue
        if lowered.startswith("/skills"):
            handle_chat_skills_command(workspace, request)
            continue
        if lowered == "/paste":
            pasted = read_paste_block()
            if not pasted:
                chat_notice("Paste", "No pasted task was captured.")
                continue
            request = pasted
        elif request.startswith("@"):
            attach_chat_file_command(workspace, tasks, task_id, request[1:].strip(), pending_context, state)
            continue
        elif request.startswith("!"):
            run_chat_command(workspace, tasks, task_id, request[1:].strip(), pending_context)
            continue
        if lowered == "/status":
            print_status_panel(workspace, task_id, args)
            continue
        if lowered == "/config":
            print_config_panel()
            continue
        if lowered == "/task":
            print_json(tasks.get(task_id))
            continue
        if lowered == "/model":
            print_model_panel(args)
            continue
        if lowered == "/runs":
            print_recent_agent_runs(workspace, limit=5)
            continue
        if lowered == "/clear":
            clear_chat_screen()
            print_chat_header(workspace, task_id, args)
            continue
        if lowered == "/cost":
            if last_report is None:
                print("no agent run yet")
            else:
                print(render_agent_cost_report(build_agent_cost_report(last_report)))
            continue

        attach_inline_file_references(workspace, tasks, task_id, request, pending_context)
        custom_prompt = expand_custom_chat_command(workspace.root, request)
        if custom_prompt is not None:
            request = custom_prompt
        request_for_agent = merge_pending_context(request, pending_context)
        pending_context.clear()
        print_chat_turn_start(request_for_agent, args)
        last_report = run_chat_agent(workspace, task_id, args, request_for_agent)
        print_chat_report(last_report)


def print_chat_header(workspace: Workspace, task_id: str, args: argparse.Namespace) -> None:
    model = primary_model(args)
    base_url = args.base_url or env_value("AKERNEL_OPENAI_BASE_URL") or ""
    api_key_set = bool(env_value("AKERNEL_OPENAI_API_KEY"))
    print_chat_home(workspace, task_id, args, model)
    if args.provider == "openai" and (not api_key_set or not base_url):
        chat_notice("Setup needed", "Run `akernel setup` before sending OpenAI-backed tasks.")


def print_chat_home(workspace: Workspace, task_id: str, args: argparse.Namespace, model: str) -> None:
    width = chat_width()
    print("")
    print(chat_color("akernel", "cyan", bold=True))
    print(chat_color("focused agent workspace", "dim"))
    print("")
    print(truncate_line(f"{compact_path(Path.cwd())}", width))
    session = f"task {task_id[:12]} | {args.provider} | {model} | {args.profile} | max {args.max_steps}"
    print(chat_color(truncate_line(session, width), "dim"))
    print("")
    print(truncate_line("Ask a task, attach @file, or run !command for checked local context.", width))
    print(chat_color(truncate_line("/help commands | /status session | /extensions tools | /cost last run | /exit", width), "dim"))


def resolve_chat_ui(args: argparse.Namespace) -> str:
    requested = getattr(args, "ui", "auto")
    if requested in {"classic", "tui"}:
        return requested
    if os.environ.get("AKERNEL_UI"):
        value = os.environ["AKERNEL_UI"].strip().lower()
        if value in {"classic", "tui"}:
            return value
    return "classic"


def run_chat_loop_tui(
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    args: argparse.Namespace,
) -> None:
    last_report: dict[str, Any] | None = None
    state: dict[str, Any] = {"last_report": None, "scroll_offset": 0, "file_matches": []}
    pending_context: list[str] = []
    transcript: list[dict[str, str]] = [
        {
            "role": "system",
            "title": "Welcome",
            "text": "Describe a task, search files with @query, run safe commands with !command, or type /help.",
        }
    ]
    use_alt_screen = (
        sys.stdout.isatty()
        and os.environ.get("AKERNEL_ALT_SCREEN", "").strip().lower() in {"1", "true", "yes"}
        and not os.environ.get("AKERNEL_NO_ALT_SCREEN")
    )
    if use_alt_screen:
        print("\033[?1049h", end="")
    state["scrollback_mode"] = not use_alt_screen
    try:
        render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="ready", state=state, clear=use_alt_screen)
        while True:
            try:
                request = read_chat_input(tui_prompt(args), workspace).strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                transcript.append({"role": "system", "title": "Interrupted", "text": "Keyboard interrupt received."})
                render_chat_tui_update(workspace, task_id, args, transcript, last_report, pending_context, status="interrupted", state=state, clear=use_alt_screen)
                break
            if not request:
                render_chat_tui_update(workspace, task_id, args, transcript, last_report, pending_context, status="ready", state=state, clear=use_alt_screen)
                continue
            lowered = request.lower()
            if lowered in {"/exit", "/quit", "exit", "quit"}:
                break
            if lowered == "/clear":
                transcript.clear()
                state["scroll_offset"] = 0
                render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="cleared", state=state, clear=use_alt_screen)
                continue
            state["last_report"] = last_report
            if handle_tui_command(
                request,
                lowered,
                workspace=workspace,
                tasks=tasks,
                task_id=task_id,
                args=args,
                pending_context=pending_context,
                transcript=transcript,
                state=state,
            ):
                last_report = state.get("last_report")
                render_chat_tui_update(workspace, task_id, args, transcript, last_report, pending_context, status="ready", state=state, clear=use_alt_screen)
                continue

            state["scroll_offset"] = 0
            attach_notice = capture_chat_output(
                lambda: attach_inline_file_references(workspace, tasks, task_id, request, pending_context)
            )
            if attach_notice:
                transcript.append({"role": "system", "title": "Attached Context", "text": attach_notice})
                render_chat_tui_message(transcript[-1])
                state["rendered_count"] = len(transcript)
            custom_prompt = expand_custom_chat_command(workspace.root, request)
            if custom_prompt is not None:
                request = custom_prompt
            request_for_agent = merge_pending_context(request, pending_context)
            pending_context.clear()
            transcript.append({"role": "user", "title": "You", "text": request_for_agent})
            render_chat_tui_message({"role": "user", "title": "You", "text": request_for_agent})
            state["rendered_count"] = len(transcript)
            render_chat_tui_status("running", args, pending_context)
            last_report = run_chat_agent(workspace, task_id, args, request_for_agent)
            transcript.append({"role": "assistant", "title": "Assistant", "text": format_tui_report(last_report)})
            render_chat_tui_message({"role": "assistant", "title": "Assistant", "text": format_tui_report(last_report)})
            state["rendered_count"] = len(transcript)
            render_chat_tui_status("ready", args, pending_context, last_report=last_report)
    finally:
        if use_alt_screen:
            print("\033[?1049l", end="")
    print("bye")


def handle_tui_command(
    request: str,
    lowered: str,
    *,
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    args: argparse.Namespace,
    pending_context: list[str],
    transcript: list[dict[str, str]],
    state: dict[str, Any],
) -> bool:
    last_report = state.get("last_report")
    if lowered in {"/up", "/pageup", "/pgup"}:
        state["scroll_offset"] = int(state.get("scroll_offset", 0)) + 12
        return True
    if lowered in {"/down", "/pagedown", "/pgdn"}:
        state["scroll_offset"] = max(0, int(state.get("scroll_offset", 0)) - 12)
        return True
    if lowered in {"/latest", "/bottom"}:
        state["scroll_offset"] = 0
        return True
    if lowered == "/help":
        transcript.append({"role": "system", "title": "Help", "text": format_chat_help_text()})
        return True
    if lowered == "/compact":
        transcript.append({"role": "system", "title": "Compact Brief", "text": capture_chat_output(lambda: print_task_brief_panel(tasks, task_id))})
        return True
    if lowered == "/commands":
        transcript.append({"role": "system", "title": "Slash Commands", "text": capture_chat_output(lambda: print_custom_commands_panel(workspace.root))})
        return True
    if lowered in {"/extensions", "/ext"}:
        transcript.append({"role": "system", "title": "Extensions", "text": capture_chat_output(lambda: print_extensions_panel(workspace))})
        return True
    if lowered == "/mcp" or lowered.startswith("/mcp "):
        transcript.append({"role": "system", "title": "MCP", "text": capture_chat_output(lambda: handle_chat_mcp_command(workspace, request))})
        return True
    if lowered == "/skills" or lowered.startswith("/skills "):
        transcript.append({"role": "system", "title": "Skills", "text": capture_chat_output(lambda: handle_chat_skills_command(workspace, request))})
        return True
    if lowered == "/status":
        transcript.append({"role": "system", "title": "Status", "text": capture_chat_output(lambda: print_status_panel(workspace, task_id, args))})
        return True
    if lowered == "/config":
        transcript.append({"role": "system", "title": "Config", "text": capture_chat_output(print_config_panel)})
        return True
    if lowered == "/task":
        transcript.append({"role": "system", "title": "Task", "text": json.dumps(tasks.get(task_id), indent=2, ensure_ascii=False)})
        return True
    if lowered == "/model":
        transcript.append({"role": "system", "title": "Model Roles", "text": capture_chat_output(lambda: print_model_panel(args))})
        return True
    if lowered == "/runs":
        transcript.append({"role": "system", "title": "Recent Runs", "text": capture_chat_output(lambda: print_recent_agent_runs(workspace, limit=5))})
        return True
    if lowered == "/cost":
        text = "no agent run yet" if last_report is None else render_agent_cost_report(build_agent_cost_report(last_report))
        transcript.append({"role": "system", "title": "Cost", "text": text})
        return True
    if lowered == "/paste":
        transcript.append({"role": "system", "title": "Paste", "text": "Paste mode uses the terminal below. Finish with /end."})
        pasted = read_paste_block()
        if pasted:
            transcript.append({"role": "user", "title": "Pasted Task", "text": pasted})
            request_for_agent = merge_pending_context(pasted, pending_context)
            pending_context.clear()
            report = run_chat_agent(workspace, task_id, args, request_for_agent)
            state["last_report"] = report
            transcript.append({"role": "assistant", "title": "Assistant", "text": format_tui_report(report)})
        return True
    if request.startswith("@"):
        text = capture_chat_output(lambda: attach_chat_file_command(workspace, tasks, task_id, request[1:].strip(), pending_context, state))
        transcript.append({"role": "system", "title": "Attach File", "text": text})
        return True
    if request.startswith("!"):
        text = capture_chat_output(lambda: run_chat_command(workspace, tasks, task_id, request[1:].strip(), pending_context))
        transcript.append({"role": "system", "title": "Command", "text": text})
        return True
    return False


def capture_chat_output(func: Any) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        func()
    return buffer.getvalue().strip()


def run_chat_agent(workspace: Workspace, task_id: str, args: argparse.Namespace, request: str) -> dict[str, Any]:
    def run_with_progress(progress_callback: Any) -> dict[str, Any]:
        return AgentLoop(workspace).run(
            request,
            provider_name=args.provider,
            budget=args.budget,
            profile=args.profile,
            model=args.model,
            aux_model=args.aux_model,
            model_routing=args.model_routing,
            aux_review=args.aux_review,
            base_url=args.base_url,
            task_id=task_id,
            max_steps=args.max_steps,
            remember=not args.no_remember,
            allow_over_budget=args.allow_over_budget,
            expect_json=args.expect_json,
            progress_callback=progress_callback,
        )
    if should_use_chat_spinner():
        return run_agent_with_spinner(run_with_progress, args)
    return run_with_progress(print_agent_progress_event)


def should_use_chat_spinner() -> bool:
    if os.environ.get("AKERNEL_NO_SPINNER"):
        return False
    if os.environ.get("AKERNEL_SPINNER", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return bool(sys.stdout.isatty())


def run_agent_with_spinner(run_func: Any, args: argparse.Namespace) -> dict[str, Any]:
    state: dict[str, Any] = {
        "message": f"starting | primary {primary_model(args)} | route {getattr(args, 'model_routing', 'primary')}",
        "done": False,
        "result": None,
        "error": None,
    }
    lock = threading.Lock()

    def progress(event: dict[str, Any]) -> None:
        with lock:
            state["message"] = spinner_message_from_event(event, args)

    def worker() -> None:
        try:
            result = run_func(progress)
        except BaseException as exc:
            with lock:
                state["error"] = exc
                state["done"] = True
            return
        with lock:
            state["result"] = result
            state["done"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    frames = "|/-\\"
    index = 0
    last_line_width = 0
    while True:
        with lock:
            done = bool(state["done"])
            message = str(state["message"])
        if done:
            break
        line = truncate_line(f"{frames[index % len(frames)]} {message}", chat_width())
        padding = " " * max(0, last_line_width - display_width(line))
        print("\r" + chat_color(line, "dim") + padding, end="", flush=True)
        last_line_width = display_width(line)
        index += 1
        time.sleep(0.12)
    thread.join()
    print("\r" + " " * max(0, last_line_width) + "\r", end="", flush=True)
    with lock:
        error = state["error"]
        result = state["result"]
    if error is not None:
        raise error
    return result


def spinner_message_from_event(event: dict[str, Any], args: argparse.Namespace) -> str:
    name = event.get("event")
    step = event.get("step", "?")
    max_steps = event.get("max_steps", "?")
    if name == "step_start":
        return f"step {step}/{max_steps}: building context"
    if name == "budget_expand":
        return (
            f"step {step}/{max_steps}: expanding budget "
            f"{event.get('old_budget')}->{event.get('new_budget')} tokens"
        )
    if name == "provider_start":
        role = event.get("model_role") or "primary"
        model = event.get("model") or primary_model(args)
        return f"step {step}/{max_steps}: waiting for {role} model {model}"
    if name == "context_ready":
        memory_count = event.get("memory_count", 0)
        skill_count = len(event.get("skills", []))
        used = event.get("estimated_used", "?")
        total = event.get("budget_total", "?")
        return f"step {step}/{max_steps}: context ready {used}/{total} tokens, memory {memory_count}, skills {skill_count}"
    if name == "action_start":
        label = event.get("label") or chat_action_label(str(event.get("action") or ""))
        target = event.get("target")
        suffix = f" {target}" if target else ""
        return f"step {step}/{max_steps}: {label}{suffix}"
    if name == "materialize_start":
        return f"step {step}/{max_steps}: {event.get('label', 'saving files')}"
    if name == "materialize_end":
        paths = event.get("paths") or []
        count = len(paths)
        return f"step {step}/{max_steps}: saved {count} file(s)"
    if name == "recovery_start":
        return f"step {step}/{max_steps}: preparing recovery context"
    if name == "recovery_end":
        return f"step {step}/{max_steps}: recovery ready ({event.get('count', 0)} file(s))"
    if name == "step_end":
        return (
            f"step {step}/{max_steps}: {event.get('status', 'done')} "
            f"action={event.get('action') or 'none'} tokens={event.get('tokens', 0)}"
        )
    return f"running | primary {primary_model(args)} | route {getattr(args, 'model_routing', 'primary')}"


def print_agent_progress_event(event: dict[str, Any]) -> None:
    name = event.get("event")
    step = event.get("step", "?")
    max_steps = event.get("max_steps", "?")
    if name == "step_start":
        print(chat_color(f"status   step {step}/{max_steps}: {event.get('message', 'starting')}", "dim"), flush=True)
        return
    if name == "provider_start":
        role = event.get("model_role") or "primary"
        model = event.get("model") or "default"
        reason = event.get("routing_reason") or ""
        suffix = f" ({reason})" if reason else ""
        print(chat_color(f"status   step {step}/{max_steps}: contacting {role} model {model}{suffix}", "dim"), flush=True)
        return
    if name == "context_ready":
        memory_count = event.get("memory_count", 0)
        skill_count = len(event.get("skills", []))
        used = event.get("estimated_used", "?")
        total = event.get("budget_total", "?")
        print(chat_color(f"status   step {step}/{max_steps}: context ready {used}/{total} tokens, memory={memory_count}, skills={skill_count}", "dim"), flush=True)
        return
    if name == "action_start":
        label = event.get("label") or chat_action_label(str(event.get("action") or ""))
        target = event.get("target")
        suffix = f" {target}" if target else ""
        print(chat_color(f"status   step {step}/{max_steps}: {label}{suffix}", "dim"), flush=True)
        return
    if name == "materialize_start":
        print(chat_color(f"status   step {step}/{max_steps}: {event.get('label', 'saving files')}", "dim"), flush=True)
        return
    if name == "materialize_end":
        paths = event.get("paths") or []
        print(chat_color(f"status   step {step}/{max_steps}: saved {len(paths)} file(s)", "dim"), flush=True)
        return
    if name == "recovery_start":
        print(chat_color(f"status   step {step}/{max_steps}: preparing recovery context", "dim"), flush=True)
        return
    if name == "recovery_end":
        print(chat_color(f"status   step {step}/{max_steps}: recovery ready ({event.get('count', 0)} file(s))", "dim"), flush=True)
        return
    if name == "budget_expand":
        old_budget = event.get("old_budget")
        new_budget = event.get("new_budget")
        used = event.get("estimated_used")
        print(chat_color(f"status   step {step}/{max_steps}: auto-expanded context budget {old_budget}->{new_budget} tokens (estimated {used})", "dim"), flush=True)
        return
    if name == "step_end":
        status = event.get("status") or "done"
        action = event.get("action") or "none"
        tokens = event.get("tokens", 0)
        print(chat_color(f"status   step {step}/{max_steps}: {status}, action={action}, tokens={tokens}", "dim"), flush=True)


def chat_action_label(action: str) -> str:
    labels = {
        "read_file": "reading file",
        "write_file": "creating or updating file",
        "patch_file": "applying file patch",
        "batch_patch": "applying multi-file patch",
        "run_command": "running command",
        "mcp_call": "calling MCP tool",
        "respond": "preparing final response",
    }
    return labels.get(action, action or "running action")


def read_chat_input(prompt: str, workspace: Workspace) -> str:
    if not should_use_prompt_toolkit():
        return input(prompt)
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.styles import Style
    except ImportError:
        return input(prompt)

    class ChatCompleter(Completer):
        def get_completions(self, document: Any, complete_event: Any) -> Any:
            text = document.text_before_cursor
            fragment = chat_completion_fragment(text)
            if not fragment:
                return
            for value, description in chat_completion_items(workspace.root, text):
                yield Completion(value, start_position=-len(fragment), display=value, display_meta=description)

    key_bindings = KeyBindings()

    @key_bindings.add("/")
    def _(event: Any) -> None:
        event.current_buffer.insert_text("/")
        if chat_completion_fragment(event.current_buffer.document.text_before_cursor):
            event.current_buffer.start_completion(select_first=False)

    @key_bindings.add("@")
    def _(event: Any) -> None:
        event.current_buffer.insert_text("@")
        if chat_completion_fragment(event.current_buffer.document.text_before_cursor):
            event.current_buffer.start_completion(select_first=False)

    session = PromptSession(
        completer=ChatCompleter(),
        complete_while_typing=True,
        complete_in_thread=True,
        complete_style=CompleteStyle.READLINE_LIKE,
        bottom_toolbar=" / opens commands   @ finds files   Tab accepts   Ctrl-C interrupts ",
        key_bindings=key_bindings,
        reserve_space_for_menu=6,
        style=Style.from_dict({"bottom-toolbar": "reverse ansiblack ansibrightcyan"}),
    )
    return session.prompt(ANSI(prompt))


def should_use_prompt_toolkit() -> bool:
    if os.environ.get("AKERNEL_NO_COMPLETION"):
        return False
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def chat_completion_fragment(text: str) -> str:
    match = re.search(r"(^|\s)([/@][^\s]*)$", text)
    return match.group(2) if match else ""


def chat_completion_items(root: Path, text: str, *, limit: int = 12) -> list[tuple[str, str]]:
    value = chat_completion_fragment(text) or text.strip()
    if value.startswith("/"):
        query = value.casefold()
        builtins = [
            (command, description)
            for command, description in CHAT_COMMANDS
            if command.casefold().startswith(query)
        ]
        custom = [
            (command, f"{spec['scope']} command: {spec['description']}")
            for command, spec in load_custom_chat_commands(root).items()
            if command.casefold().startswith(query)
        ]
        return (builtins + custom)[:limit]
    if value.startswith("@"):
        query = value[1:].strip()
        matches = find_workspace_files(root, query, limit=limit)
        return [(f"@{path}", "attach file") for path in matches]
    return []


def print_chat_turn_start(request: str, args: argparse.Namespace) -> None:
    preview = request if len(request) <= chat_width() - 14 else request[: chat_width() - 17] + "..."
    print("")
    print(chat_rule("Task"))
    print(truncate_line(preview, chat_width()))
    runtime = f"{args.provider} | {primary_model(args)} | route={args.model_routing} | max {args.max_steps} steps"
    print(chat_color(truncate_line(runtime, chat_width()), "dim"))
    print(chat_color("assembling context...", "dim"), flush=True)


def print_chat_report(report: dict[str, Any]) -> None:
    print("")
    final_response = report.get("final_response")
    if final_response:
        print(wrap_chat_text(str(final_response)))
        print("")
    elif report.get("status") != "responded":
        diagnostic = report.get("diagnostic")
        if isinstance(diagnostic, dict) and diagnostic:
            message = diagnostic.get("message") or diagnostic.get("category") or report.get("status")
        else:
            message = report.get("status", "run finished")
        print(wrap_chat_text(str(message)))
        print("")

    summary_line = (
        f"{report['status']} | {len(report['steps'])}/{report['max_steps']} steps | "
        f"{report['totals']['total_tokens']} tokens | agent_run: {str(report['id'])[:12]} | /cost for details"
    )
    print(chat_color(truncate_line(summary_line, chat_width()), "dim"))

    actions = [str((step.get("action") or {}).get("action") or "none") for step in report.get("steps", [])]
    if actions and actions != ["respond"]:
        print(chat_color(truncate_line("actions: " + " -> ".join(actions), chat_width()), "dim"))

    review_text = aux_review_summary(report)
    if review_text:
        print(chat_color(truncate_line(review_text, chat_width()), "dim"))

    state_info = report.get("state", {})
    if state_info.get("enabled") and state_info.get("written_count", 0):
        print(chat_color(f"memory: wrote {state_info['written_count']} record(s)", "dim"))

    diagnostic = report.get("diagnostic")
    if isinstance(diagnostic, dict) and diagnostic.get("suggestion"):
        print(chat_color(truncate_line(f"next: {diagnostic['suggestion']}", chat_width()), "yellow"))


def print_chat_help() -> None:
    for title, rows in chat_help_groups():
        chat_panel(title, rows)
    print(chat_color("Tip     Ask one concrete task at a time; Context Kernel keeps the packet lean.", "dim"))


def print_recent_agent_runs(workspace: Workspace, *, limit: int) -> None:
    reports = list_agent_reports(workspace)[:limit]
    if not reports:
        chat_notice("Recent Runs", "No agent runs yet.")
        return
    print(chat_rule("Recent Runs"))
    for report in reports:
        request = str(report.get("request", ""))
        if len(request) > 52:
            request = request[:49] + "..."
        print(
            f"  {report['id']}  "
            f"{report.get('status', ''):<10}  "
            f"steps={len(report.get('steps', [])):<2}  "
            f"tokens={report.get('totals', {}).get('total_tokens', 0):<5}  "
            f"{request}"
        )


def read_paste_block() -> str:
    print(chat_color("Paste mode. Finish with /end on its own line.", "dim"))
    lines: list[str] = []
    while True:
        try:
            line = input(chat_color("paste> ", "dim"))
        except EOFError:
            break
        if line.strip().lower() == "/end":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def merge_pending_context(request: str, pending_context: list[str]) -> str:
    if not pending_context:
        return request
    context = "\n\n".join(pending_context)
    return (
        "Use the attached local context below when helpful. "
        "Do not assume it is complete if the task needs more evidence.\n\n"
        f"{context}\n\nUser task:\n{request}"
    )


def print_task_brief_panel(tasks: TaskStore, task_id: str) -> None:
    brief = tasks.brief(task_id)
    rows = [
        ("task", brief["task"]["id"]),
        ("title", brief["task"]["title"]),
        ("status", brief["task"]["status"]),
        ("budget", f"{brief.get('estimated_tokens', 0)} tokens {ascii_meter(int(brief.get('estimated_tokens', 0) or 0), 1200, 16)}"),
        ("recent_steps", str(len(brief.get("recent_steps", [])))),
        ("run_traces", str(len(brief.get("linked_run_traces", [])))),
        ("tool_traces", str(len(brief.get("linked_tool_traces", [])))),
        ("memories", str(len(brief.get("linked_memory", [])))),
    ]
    chat_panel("Compact Brief", rows)
    latest = brief.get("recent_steps", [])[-3:]
    if latest:
        print(chat_color("Recent", "cyan"))
        for step in latest:
            print(wrap_chat_text(f"{step.get('kind')}: {step.get('note')}", indent="  "))


def print_model_panel(args: argparse.Namespace) -> None:
    chat_panel(
        "Model Roles",
        [
            ("provider", args.provider),
            ("primary", primary_model(args)),
            ("auxiliary", auxiliary_model(args)),
            ("routing", "primary by default; auto can delegate low/medium first-step planning"),
            ("review_role", "auxiliary can review primary-model steps before tool action"),
            ("mode", args.model_routing),
            ("review", args.aux_review),
            ("base_url", args.base_url or env_value("AKERNEL_OPENAI_BASE_URL") or "default"),
        ],
    )


def print_status_panel(workspace: Workspace, task_id: str, args: argparse.Namespace) -> None:
    summary = extension_summary(workspace)
    chat_panel(
        "Status Runway",
        [
            ("cwd", compact_path(Path.cwd())),
            ("workspace", compact_path(workspace.root)),
            ("task", task_id),
            ("provider", args.provider),
            ("profile", args.profile),
            ("state", workspace_state_summary(workspace)),
        ],
    )
    chat_panel(
        "Runtime Deck",
        [
            ("models", f"primary {primary_model(args)} | aux {auxiliary_model(args)}"),
            ("route", f"{args.model_routing} | review {args.aux_review}"),
            ("extensions", f"{summary['skills']} skills | {summary['mcp_enabled']}/{summary['mcp_total']} mcp | {summary['mcp_tools']} tools"),
            ("next", "attach context with @, inspect /compact, or run a concrete task"),
        ],
    )


def print_config_panel() -> None:
    chat_panel(
        "Config Runway",
        [
            ("setup", "akernel setup"),
            ("env", "AKERNEL_OPENAI_API_KEY, AKERNEL_OPENAI_BASE_URL"),
            ("models", "AKERNEL_OPENAI_MODEL, AKERNEL_OPENAI_AUX_MODEL"),
            ("network", "AKERNEL_OPENAI_TIMEOUT_SECONDS, AKERNEL_OPENAI_MAX_RETRIES"),
            ("scope", "current project .env first, installed Context Kernel .env fallback"),
        ],
    )


def chat_prompt(args: argparse.Namespace) -> str:
    model = primary_model(args)
    return "\n" + chat_color("akernel", "cyan", bold=True) + chat_color(f" [{model}]", "dim") + "> "


def clear_chat_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")
    else:
        print("\n" * 30)


def print_project_scan_summary(profile: dict[str, Any], *, config_updated: bool) -> None:
    print(f"project_profile: {profile.get('root')}")
    print(f"languages: {', '.join(profile.get('languages', [])) or 'unknown'}")
    managers = profile.get("package_managers", [])
    print(f"package_managers: {', '.join(managers) if managers else 'none'}")
    commands = profile.get("commands", {})
    if commands:
        for name, command in commands.items():
            print(f"command_{name}: {command}")
    else:
        print("commands: none")
    print(f"key_files: {', '.join(profile.get('key_files', [])[:8]) or 'none'}")
    instructions = profile.get("instructions", [])
    print(f"instructions: {', '.join(item.get('path', '') for item in instructions[:4]) if instructions else 'none'}")
    print(f"command_roots: {', '.join(profile.get('command_roots', [])[:16])}")
    print(f"config_updated: {config_updated}")


def cmd_agent_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    reports = list_agent_reports(workspace)
    if not reports:
        print("no agent runs")
        return
    for report in reports:
        print(
            f"{report['id']}\t"
            f"{report.get('created_at', '')}\t"
            f"{report.get('status', '')}\t"
            f"steps={len(report.get('steps', []))}\t"
            f"task={report.get('task_id', '')}\t"
            f"{report.get('request', '')}"
        )


def cmd_agent_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    print_json(load_agent_report(workspace, args.run_id))


def cmd_agent_cost(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    cost = build_agent_cost_report(load_agent_report(workspace, args.run_id))
    if args.json:
        print_json(cost)
        return
    print(render_agent_cost_report(cost))


def cmd_models(args: argparse.Namespace) -> None:
    for model in list_provider_models(args.provider, base_url=args.base_url):
        print(model)


def cmd_doctor(args: argparse.Namespace) -> None:
    workspace = Workspace(Path(args.workspace))
    config = workspace.load_config()
    command_policy = summarize_command_policy(workspace)
    base_url = env_value("AKERNEL_OPENAI_BASE_URL")
    api_key = env_value("AKERNEL_OPENAI_API_KEY")
    model = env_value("AKERNEL_OPENAI_MODEL") or DEFAULT_PRIMARY_MODEL
    aux_model = env_value("AKERNEL_OPENAI_AUX_MODEL") or DEFAULT_AUXILIARY_MODEL
    timeout_seconds = env_value("AKERNEL_OPENAI_TIMEOUT_SECONDS") or "180"
    max_retries = env_value("AKERNEL_OPENAI_MAX_RETRIES") or "3"
    retry_backoff_seconds = env_value("AKERNEL_OPENAI_RETRY_BACKOFF_SECONDS") or "1.5"
    print(f"project_root: {Path.cwd().resolve()}")
    print(f"workspace: {workspace.root}")
    print(f"workspace_initialized: {workspace.state.exists()}")
    print(f"workspace_config: {workspace.config_file}")
    print(f"workspace_config_version: {config.get('version')}")
    print(f"project_env_api_key_set: {bool(api_key)}")
    print(f"project_env_base_url: {normalize_openai_base_url(base_url or '') if base_url else ''}")
    print(f"project_env_primary_model: {model}")
    print(f"project_env_auxiliary_model: {aux_model}")
    print(f"project_env_timeout_seconds: {timeout_seconds}")
    print(f"project_env_max_retries: {max_retries}")
    print(f"project_env_retry_backoff_seconds: {retry_backoff_seconds}")
    profile = load_project_profile(workspace)
    print(f"project_profile: {workspace.project_file if profile else ''}")
    print(f"project_summary: {profile.get('summary', '') if profile else ''}")
    print(f"command_allowed_roots: {', '.join(command_policy['allowed_roots'])}")
    print(f"command_blocked_terms: {', '.join(command_policy['blocked_terms'])}")


def cmd_plan(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_resume_args(args)
    plan = ExecutionPlanner(workspace).plan(
        args.request,
        args.budget,
        args.profile,
        task_id=args.task,
        resume=args.resume,
    )
    if args.json:
        print_json(plan)
        return

    print(f"request: {plan['request']}")
    print(f"profile: {plan['profile']}")
    print(f"route: {plan['route']['mode']} ({plan['route']['complexity']})")
    if plan["task"]["resume"]:
        print(f"task: {plan['task']['id']} resume tokens={plan['task']['estimated_tokens']}")
    print(f"tokens: used={plan['budget']['estimated_used']} total={plan['budget']['total']} remaining={plan['budget']['estimated_remaining']}")
    print(f"savings: {plan['savings']['estimated_tokens']} tokens ({plan['savings']['percent']}%)")
    print(f"selected: memory={len(plan['selection']['memory'])} skills={len(plan['selection']['skills'])}")
    print(f"policy: {'review required' if plan['policy']['requires_policy_check'] else 'clear'}")
    print(f"command_roots: {', '.join(plan['policy']['command_policy']['allowed_roots'])}")
    if plan["warnings"]:
        print("warnings:")
        for warning in plan["warnings"]:
            print(f"- {warning}")


def cmd_policy_file(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    result = check_file_policy(
        workspace,
        args.operation,
        args.path,
        allow_destructive=args.allow_destructive,
    )
    print_policy_result(result, args.json)


def cmd_policy_command(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args, initialized=False)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    result = check_command_policy(
        " ".join(command),
        workspace=workspace if workspace.state.exists() else None,
        allow_destructive=args.allow_destructive,
    )
    print_policy_result(result, args.json)


def cmd_task_start(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    task = TaskStore(workspace).start(args.title, args.goal, with_plan=args.plan)
    print(f"task: {task['id']} active {task['title']}")
    if task.get("plan"):
        active = task["plan"]["milestones"][0]
        print(f"plan: {len(task['plan']['milestones'])} milestones active={active['id']} {active['title']}")


def cmd_task_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    tasks = TaskStore(workspace).list(status=args.status)
    if not tasks:
        print("no tasks")
        return
    for task in tasks:
        print(f"{task['id']}\t{task['status']}\tsteps={len(task['steps'])}\t{task['title']}")


def cmd_task_status(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    task = TaskStore(workspace).get(args.task_id)
    if args.json:
        print_json(task)
        return
    print(f"task: {task['id']}")
    print(f"title: {task['title']}")
    print(f"status: {task['status']}")
    print(f"steps: {len(task['steps'])}")
    print(
        "refs: "
        f"runs={len(task['refs']['run_traces'])} "
        f"tools={len(task['refs']['tool_traces'])} "
        f"memories={len(task['refs']['memories'])}"
    )
    if task.get("plan"):
        progress = task_plan_progress_text(task["plan"])
        active = next((item for item in task["plan"]["milestones"] if item.get("status") == "active"), None)
        print(f"plan: {progress}")
        if active:
            print(f"active: {active['id']} {active['title']}")
    if task["steps"]:
        latest = task["steps"][-1]
        print(f"latest: [{latest['kind']}] {latest['note']}")


def cmd_task_brief(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    brief = TaskStore(workspace).brief(args.task_id)
    if args.json:
        print_json(brief)
        return
    task = brief["task"]
    print(f"task: {task['id']} {task['status']}")
    print(f"title: {task['title']}")
    print(f"goal: {task['goal']}")
    print(f"estimated_tokens: {brief['estimated_tokens']}")
    print(f"recent_steps: {len(brief['recent_steps'])}")
    for step in brief["recent_steps"][-3:]:
        print(f"- [{step['kind']}] {step['note']}")
    if brief.get("plan"):
        plan = brief["plan"]
        progress = plan["progress"]
        active = plan.get("active_milestone")
        print(
            "plan: "
            f"{progress['completed']}/{progress['total']} completed "
            f"blocked={progress['blocked']} skipped={progress['skipped']}"
        )
        if active:
            print(f"active: {active['id']} {active['title']}")
    print(
        "linked: "
        f"memory={len(brief['linked_memory'])} "
        f"runs={len(brief['linked_run_traces'])} "
        f"tools={len(brief['linked_tool_traces'])}"
    )


def cmd_task_plan(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    task = TaskStore(workspace).plan(args.task_id, goal=args.goal, force=args.force)
    if args.json:
        print_json(task["plan"])
        return
    print_task_plan(task)


def cmd_task_next(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    checkpoint = TaskStore(workspace).next_checkpoint(args.task_id)
    if args.json:
        print_json(checkpoint)
        return
    milestone = checkpoint.get("milestone")
    print(f"task: {checkpoint['task_id']} {checkpoint['task_status']}")
    print(f"progress: {checkpoint['plan_progress']['completed']}/{checkpoint['plan_progress']['total']} completed")
    if milestone:
        print(f"next: {milestone['id']} {milestone['title']} [{milestone['status']}]")
        print(f"objective: {milestone['objective']}")
        for item in milestone.get("acceptance", []):
            print(f"- {item}")
    print(f"resume: {checkpoint['resume_prompt']}")


def cmd_task_checkpoint(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    task = TaskStore(workspace).checkpoint(
        args.task_id,
        args.note,
        milestone_id=args.milestone,
        status=args.status,
    )
    print(f"task: {task['id']} checkpoint added steps={len(task['steps'])}")
    if task.get("plan"):
        print(f"plan: {task_plan_progress_text(task['plan'])}")


def print_task_plan(task: dict[str, Any]) -> None:
    plan = task["plan"]
    print(f"task: {task['id']}")
    print(f"objective: {plan['objective']}")
    print(f"progress: {task_plan_progress_text(plan)}")
    for milestone in plan.get("milestones", []):
        print(f"- {milestone['id']} [{milestone['status']}] {milestone['title']}")
        print(f"  objective: {milestone['objective']}")


def task_plan_progress_text(plan: dict[str, Any]) -> str:
    milestones = plan.get("milestones", [])
    total = len(milestones)
    completed = sum(1 for item in milestones if item.get("status") == "completed")
    blocked = sum(1 for item in milestones if item.get("status") == "blocked")
    skipped = sum(1 for item in milestones if item.get("status") == "skipped")
    return f"{completed}/{total} completed blocked={blocked} skipped={skipped}"


def attach_run_to_task(workspace: Workspace, task_id: str, trace: dict[str, Any]) -> None:
    store = TaskStore(workspace)
    store.attach(task_id, "run", trace["id"])
    for record in trace.get("state", {}).get("records", []):
        store.attach(task_id, "memory", record["id"])


def ensure_task_attachable(workspace: Workspace, task_id: str | None) -> None:
    if not task_id:
        return
    task = TaskStore(workspace).get(task_id)
    if task.get("status") == "completed":
        raise ValueError(f"Task is completed and cannot receive new traces: {task_id}")


def ensure_resume_args(args: argparse.Namespace) -> None:
    if getattr(args, "resume", False) and not getattr(args, "task", None):
        raise ValueError("--resume requires --task <task-id>")


def attach_tool_to_task_if_requested(workspace: Workspace, task_id: str | None, result: dict[str, Any]) -> None:
    if not task_id:
        return
    TaskStore(workspace).attach(task_id, "tool", result["id"])


def cmd_task_step(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    task = TaskStore(workspace).step(args.task_id, args.note)
    print(f"task: {task['id']} step added steps={len(task['steps'])}")


def cmd_task_attach(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    task = TaskStore(workspace).attach(args.task_id, args.kind, args.ref_id)
    print(f"task: {task['id']} attached {args.kind}:{args.ref_id}")


def cmd_task_block(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    task = TaskStore(workspace).set_status(args.task_id, "blocked", note=args.note)
    print(f"task: {task['id']} blocked")


def cmd_task_complete(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    task = TaskStore(workspace).set_status(args.task_id, "completed", note=args.note)
    print(f"task: {task['id']} completed")


def cmd_eval_run(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    report = EvalRunner(workspace).run_fixture(
        Path(args.fixture),
        default_budget=args.budget,
        default_profile=args.profile,
        save=not args.no_save,
        execute_provider=args.provider if args.execute else None,
        execute_model=args.model,
        execute_base_url=args.base_url,
    )
    if args.json:
        print_json(report)
        return

    summary = report["summary"]
    print(f"report: {report['id']}")
    print(f"fixture: {report['fixture']}")
    print(f"tasks: {summary['task_count']}")
    print(f"profile: {args.profile}")
    print(f"avg_savings: {summary['average_savings_percent']}%")
    print(f"total_kernel_tokens: {summary['total_kernel_tokens']}")
    print(f"total_baseline_tokens: {summary['total_baseline_tokens']}")
    if summary["executed_tasks"]:
        print(f"executed_tasks: {summary['executed_tasks']}")
        print(f"execution_tokens: {summary['total_execution_tokens']}")
    if summary.get("blocked_tasks"):
        print(f"blocked_tasks: {summary['blocked_tasks']}")
    print(f"checks: {summary['passed_checks']}/{summary['total_checks']}")
    for task in report["tasks"]:
        execution = task.get("execution", {})
        execution_text = f"\texec_tokens={execution.get('total_tokens')}" if execution else ""
        print(
            f"{task['id']}\t"
            f"savings={task['savings']['percent']}%\t"
            f"kernel={task['kernel']['estimated_tokens']}\t"
            f"baseline={task['baseline']['estimated_tokens']}\t"
            f"checks={task['checks']['passed']}/{task['checks']['total']}"
            f"{execution_text}"
        )


def cmd_eval_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    reports = EvalRunner(workspace).list_reports()
    if not reports:
        print("no eval reports")
        return
    for report in reports:
        print(
            f"{report['id']}\t"
            f"{report['created_at']}\t"
            f"tasks={report['task_count']}\t"
            f"avg_savings={report['average_savings_percent']}%\t"
            f"checks={report['checks']}\t"
            f"{report['name']}"
        )


def cmd_eval_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    print_json(EvalRunner(workspace).get_report(args.report_id))


def cmd_eval_cost(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    cost = build_eval_cost_report(EvalRunner(workspace).get_report(args.report_id))
    if args.json:
        print_json(cost)
        return
    print(render_cost_report(cost))


def cmd_eval_diff(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    diff = EvalRunner(workspace).diff_reports(args.before_id, args.after_id)
    if args.json:
        print_json(diff)
        enforce_regression_gate(diff, enabled=args.fail_on_regression, label="eval diff")
        return

    summary = diff["summary_delta"]
    print(f"before: {diff['before']['id']}")
    print(f"after: {diff['after']['id']}")
    print(f"kernel_tokens_delta: {summary['kernel_tokens']}")
    print(f"baseline_tokens_delta: {summary['baseline_tokens']}")
    print(f"savings_tokens_delta: {summary['savings_tokens']}")
    print(f"savings_percent_delta: {summary['savings_percent']}")
    print(f"checks_delta: {summary['passed_checks']}/{summary['total_checks']}")
    print(f"regressions: {len(diff['regressions'])}")
    cost_diff = diff.get("cost_diff", {})
    if cost_diff:
        hotspot = cost_diff.get("hotspot_change", {})
        weakest = cost_diff.get("weakest_savings_change", {})
        print(f"cost_regressions: {len(diff.get('cost_regressions', []))}")
        print(
            f"hotspot_delta: {hotspot.get('before_scope', '')} -> {hotspot.get('after_scope', '')} "
            f"({hotspot.get('metric_delta', 0)})"
        )
        print(
            f"weakest_savings_delta: {weakest.get('before_scope', '')} -> {weakest.get('after_scope', '')} "
            f"({weakest.get('metric_delta', 0)})"
        )
    for task in diff["tasks"]:
        if task["status"] != "changed":
            print(f"{task['id']}\t{task['status']}")
            continue
        print(
            f"{task['id']}\t"
            f"kernel_delta={task['kernel_token_delta']}\t"
            f"savings_delta={task['savings_percent_delta']}%\t"
            f"checks_delta={task['passed_check_delta']}/{task['total_check_delta']}"
        )
    enforce_regression_gate(diff, enabled=args.fail_on_regression, label="eval diff")


def cmd_bench_run(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    report = BenchmarkRunner(workspace).run_directory(
        Path(args.directory),
        default_budget=args.budget,
        default_profile=args.profile,
        save=not args.no_save,
        execute_provider=args.provider if args.execute else None,
        execute_model=args.model,
        execute_base_url=args.base_url,
    )
    if args.json:
        print_json(report)
        return

    print_benchmark_report(report)


def cmd_bench_gate(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    runner = BenchmarkRunner(workspace)
    report = runner.run_directory(
        Path(args.directory),
        default_budget=args.budget,
        default_profile=args.profile,
        save=True,
        execute_provider=args.provider if args.execute else None,
        execute_model=args.model,
        execute_base_url=args.base_url,
    )
    baseline_result = runner.find_baseline(
        Path(args.directory),
        baseline_id=args.baseline_report,
        exclude_id=report["id"],
    )

    if baseline_result is None:
        report_ok = benchmark_report_ok(report)
        result = {
            "status": "missing_baseline" if report_ok else "failed",
            "report": report,
            "report_ok": report_ok,
            "baseline": None,
            "baseline_match": None,
            "diff": None,
        }
        if args.json:
            print_json(result)
        else:
            print(f"report: {report['id']}")
            print(f"benchmark: {report['benchmark']}")
            print("baseline: none")
            print(f"status: {result['status']}")
            print_benchmark_check_summary(report)
            print("note: no saved benchmark report matched this directory")
        enforce_benchmark_report_gate(report, label="benchmark gate")
        if args.require_baseline:
            raise RuntimeError(f"benchmark gate could not find baseline for {Path(args.directory)}")
        return

    baseline = baseline_result["report"]
    diff = runner.diff_reports(baseline["id"], report["id"])
    report_ok = benchmark_report_ok(report)
    result = {
        "status": "passed" if diff.get("ok", False) and report_ok else "failed",
        "report": report,
        "report_ok": report_ok,
        "baseline": benchmark_ref(baseline),
        "baseline_match": baseline_result["match"],
        "diff": diff,
    }
    if args.json:
        print_json(result)
        enforce_benchmark_report_gate(report, label="benchmark gate")
        enforce_regression_gate(diff, enabled=True, label="benchmark gate")
        return

    print(f"report: {report['id']}")
    print(f"benchmark: {report['benchmark']}")
    print(f"baseline: {baseline['id']}")
    print(f"baseline_match: {baseline_result['match']}")
    print(f"status: {result['status']}")
    print_benchmark_check_summary(report)
    print_benchmark_diff(diff)
    enforce_benchmark_report_gate(report, label="benchmark gate")
    enforce_regression_gate(diff, enabled=True, label="benchmark gate")


def cmd_bench_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    reports = BenchmarkRunner(workspace).list_reports()
    if not reports:
        print("no benchmark reports")
        return
    for report in reports:
        print(
            f"{report['id']}\t"
            f"{report['created_at']}\t"
            f"fixtures={report['fixture_count']}\t"
            f"tasks={report['task_count']}\t"
            f"avg_savings={report['average_savings_percent']}%\t"
            f"checks={report['checks']}\t"
            f"{report['name']}"
        )


def cmd_bench_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    print_json(BenchmarkRunner(workspace).get_report(args.report_id))


def cmd_bench_cost(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    cost = build_benchmark_cost_report(BenchmarkRunner(workspace).get_report(args.report_id))
    if args.json:
        print_json(cost)
        return
    print(render_cost_report(cost))


def cmd_bench_diff(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    diff = BenchmarkRunner(workspace).diff_reports(args.before_id, args.after_id)
    if args.json:
        print_json(diff)
        enforce_regression_gate(diff, enabled=args.fail_on_regression, label="benchmark diff")
        return

    print_benchmark_diff(diff)
    enforce_regression_gate(diff, enabled=args.fail_on_regression, label="benchmark diff")


def cmd_bench_export(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    output = Path(args.output) if args.output else None
    path = BenchmarkRunner(workspace).export_markdown(args.report_id, output=output)
    print(f"exported: {path}")


def cmd_bench_evidence(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    runner = BenchmarkRunner(workspace)
    report_ids = args.report_ids or None
    evidence = runner.evidence(report_ids, limit=args.limit)
    if args.output:
        path = runner.export_evidence_markdown(report_ids, limit=args.limit, output=Path(args.output))
        print(f"exported: {path}")
    if args.json:
        print_json(evidence)
    elif not args.output:
        print(render_benchmark_evidence_markdown(evidence).rstrip())
    if args.fail_under is not None and evidence["total_savings_percent"] < args.fail_under:
        raise SystemExit(
            f"benchmark evidence below threshold: {evidence['total_savings_percent']}% < {args.fail_under}%"
        )


def cmd_trace_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    paths = sorted(workspace.traces_dir.glob("*.json"))
    if not paths:
        print("no traces")
        return
    for path in paths:
        trace = Workspace.read_json(path)
        response = trace.get("response", {})
        print(f"{trace['id']}\t{trace['created_at']}\t{trace['provider']}\ttokens={response.get('total_tokens')}\t{trace['request']}")


def cmd_trace_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    path = workspace.traces_dir / f"{args.trace_id}.json"
    if not path.exists():
        raise KeyError(f"Unknown trace: {args.trace_id}")
    print_json(Workspace.read_json(path))


def cmd_trace_verify(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    path = workspace.traces_dir / f"{args.trace_id}.json"
    if not path.exists():
        raise KeyError(f"Unknown trace: {args.trace_id}")
    result = verify_trace(Workspace.read_json(path), expect_json=args.expect_json)
    print_json(result)


def cmd_trace_remember(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    path = workspace.traces_dir / f"{args.trace_id}.json"
    if not path.exists():
        raise KeyError(f"Unknown trace: {args.trace_id}")
    trace = Workspace.read_json(path)
    writer = StateWriter(workspace)
    if args.dry_run:
        print_json({"enabled": False, "candidates": writer.propose_from_trace(trace)})
        return
    result = writer.write_from_trace(trace)
    trace["state"] = result
    Workspace.write_json(path, trace)
    print(f"state: wrote {result['written_count']} memory record(s)")


if __name__ == "__main__":
    main()
