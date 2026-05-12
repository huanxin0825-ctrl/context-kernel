from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from getpass import getpass
import io
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from .agent_reports import build_agent_cost_report, load_agent_report, render_agent_cost_report
from .benchmarks import BenchmarkRunner, benchmark_ref
from .budget import DEFAULT_PROFILE, profile_names
from .context import ContextBuilder
from .evals import EvalRunner
from .global_memory import pull_global_memories, push_global_memories
from .loop import AgentLoop, summarize_tool_result
from .marketplace import install_marketplace_skill, is_remote_reference, list_marketplace_skills
from .memory import ALLOWED_KINDS, MemoryStore
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
from .tools import ToolExecutor
from .verifier import verify_trace


DEFAULT_PRIMARY_MODEL = "gpt-5.5"
DEFAULT_AUXILIARY_MODEL = "gpt-5.3-codex"
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
    agent_run.add_argument("--model-routing", choices=["auto", "primary", "auxiliary"], default="auto", help="Choose how agent steps select primary vs auxiliary model.")
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

    chat_parser = subparsers.add_parser("chat", help="Start an interactive Claude Code-style task session.")
    add_provider_args(chat_parser)
    add_budget_args(chat_parser)
    chat_parser.add_argument("--task", default=None, help="Continue an existing task session.")
    chat_parser.add_argument("--title", default="Interactive chat", help="Title for a new task session.")
    chat_parser.add_argument("--max-steps", type=int, default=5, help="Maximum agent loop steps per user message.")
    chat_parser.add_argument("--model-routing", choices=["auto", "primary", "auxiliary"], default="auto", help="Choose how agent steps select primary vs auxiliary model.")
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
    tool_read = tool_sub.add_parser("read", help="Read a workspace file through policy.")
    tool_read.add_argument("path")
    tool_read.add_argument("--max-chars", type=int, default=8000)
    tool_read.add_argument("--json", action="store_true")
    tool_read.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_read.set_defaults(func=cmd_tool_read)
    tool_write = tool_sub.add_parser("write", help="Write a workspace file through policy.")
    tool_write.add_argument("path")
    tool_write.add_argument("--text", required=True)
    tool_write.add_argument("--json", action="store_true")
    tool_write.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_write.set_defaults(func=cmd_tool_write)
    tool_patch = tool_sub.add_parser("patch", help="Patch a workspace file with structured replacement modes.")
    tool_patch.add_argument("path")
    tool_patch.add_argument("--old")
    tool_patch.add_argument("--new", required=True)
    tool_patch.add_argument("--replace-all", action="store_true", help="Replace every match of --old instead of requiring a single match.")
    tool_patch.add_argument("--occurrence", type=int, default=None, help="Replace only the nth match of --old.")
    tool_patch.add_argument("--start-anchor", default=None, help="Replace the block that starts after this anchor.")
    tool_patch.add_argument("--end-anchor", default=None, help="Replace the block that ends before this anchor.")
    tool_patch.add_argument("--include-anchors", action="store_true", help="Replace the anchors together with the block body.")
    tool_patch.add_argument("--json", action="store_true")
    tool_patch.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_patch.set_defaults(func=cmd_tool_patch)
    tool_batch_patch = tool_sub.add_parser("batch-patch", help="Apply multiple structured patches from a JSON spec file.")
    tool_batch_patch.add_argument("--specs-file", required=True, help="JSON file containing an array of patch specs, or an object with an edits array.")
    tool_batch_patch.add_argument("--json", action="store_true")
    tool_batch_patch.add_argument("--task", default=None, help="Attach the batch trace to a task session.")
    tool_batch_patch.set_defaults(func=cmd_tool_batch_patch)
    tool_delete = tool_sub.add_parser("delete", help="Delete a workspace file through destructive policy.")
    tool_delete.add_argument("path")
    tool_delete.add_argument("--allow-destructive", action="store_true")
    tool_delete.add_argument("--json", action="store_true")
    tool_delete.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_delete.set_defaults(func=cmd_tool_delete)
    tool_exec = tool_sub.add_parser("exec", help="Run a safe command through policy.")
    tool_exec.add_argument("--allow-destructive", action="store_true")
    tool_exec.add_argument("--timeout", type=int, default=30)
    tool_exec.add_argument("--json", action="store_true")
    tool_exec.add_argument("--task", default=None, help="Attach the tool trace to a task session.")
    tool_exec.add_argument("command", nargs=argparse.REMAINDER)
    tool_exec.set_defaults(func=cmd_tool_exec)
    tool_list = tool_sub.add_parser("list", help="List tool execution traces.")
    tool_list.set_defaults(func=cmd_tool_list)
    tool_show = tool_sub.add_parser("show", help="Show a tool execution trace.")
    tool_show.add_argument("trace_id")
    tool_show.set_defaults(func=cmd_tool_show)

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
        current_key = existing.get("CONTEXT_KERNEL_OPENAI_API_KEY", "")
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
        default_base_url = existing.get("CONTEXT_KERNEL_OPENAI_BASE_URL") or "https://clarmy.cloud/v1"
        base_url = prompt_text("Base URL", default_base_url, interactive=interactive)
    base_url = normalize_openai_base_url(base_url)

    model = args.model
    if model is None:
        default_model = existing.get("CONTEXT_KERNEL_OPENAI_MODEL") or DEFAULT_PRIMARY_MODEL
        model = prompt_text("Primary model", default_model, interactive=interactive)

    aux_model = args.aux_model
    if aux_model is None:
        default_aux_model = existing.get("CONTEXT_KERNEL_OPENAI_AUX_MODEL") or DEFAULT_AUXILIARY_MODEL
        aux_model = prompt_text("Auxiliary model", default_aux_model, interactive=interactive)

    write_project_env(env_path, api_key=api_key, base_url=base_url, model=model, aux_model=aux_model)
    print(f"configured: {env_path}")
    print("api_key: set")
    print(f"base_url: {base_url}")
    print(f"primary_model: {model}")
    print(f"auxiliary_model: {aux_model}")
    if args.verify:
        previous = {
            "CONTEXT_KERNEL_OPENAI_API_KEY": os.environ.get("CONTEXT_KERNEL_OPENAI_API_KEY"),
            "CONTEXT_KERNEL_OPENAI_BASE_URL": os.environ.get("CONTEXT_KERNEL_OPENAI_BASE_URL"),
            "CONTEXT_KERNEL_OPENAI_MODEL": os.environ.get("CONTEXT_KERNEL_OPENAI_MODEL"),
            "CONTEXT_KERNEL_OPENAI_AUX_MODEL": os.environ.get("CONTEXT_KERNEL_OPENAI_AUX_MODEL"),
        }
        try:
            os.environ["CONTEXT_KERNEL_OPENAI_API_KEY"] = api_key
            os.environ["CONTEXT_KERNEL_OPENAI_BASE_URL"] = base_url
            os.environ["CONTEXT_KERNEL_OPENAI_MODEL"] = model
            os.environ["CONTEXT_KERNEL_OPENAI_AUX_MODEL"] = aux_model
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


def write_project_env(path: Path, *, api_key: str, base_url: str, model: str, aux_model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"CONTEXT_KERNEL_OPENAI_API_KEY={api_key}",
        f"CONTEXT_KERNEL_OPENAI_BASE_URL={base_url}",
        f"CONTEXT_KERNEL_OPENAI_MODEL={model}",
        f"CONTEXT_KERNEL_OPENAI_AUX_MODEL={aux_model}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def restore_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


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
    state: dict[str, Any] = {"last_report": None}
    pending_context: list[str] = []
    if resolve_chat_ui(args) == "tui":
        run_chat_loop_tui(workspace, tasks, task_id, args)
        return

    print_chat_header(workspace, task_id, args)
    while True:
        try:
            request = input(chat_prompt(args)).strip()
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
        if lowered == "/paste":
            pasted = read_paste_block()
            if not pasted:
                chat_notice("Paste", "No pasted task was captured.")
                continue
            request = pasted
        elif request.startswith("@"):
            attach_chat_file(workspace, tasks, task_id, request[1:].strip(), pending_context)
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

        request_for_agent = merge_pending_context(request, pending_context)
        pending_context.clear()
        print_chat_turn_start(request_for_agent, args)
        last_report = AgentLoop(workspace).run(
            request_for_agent,
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
        )
        print_chat_report(last_report)


def print_chat_header(workspace: Workspace, task_id: str, args: argparse.Namespace) -> None:
    model = primary_model(args)
    base_url = args.base_url or env_value("CONTEXT_KERNEL_OPENAI_BASE_URL") or ""
    api_key_set = bool(env_value("CONTEXT_KERNEL_OPENAI_API_KEY"))
    chat_banner(
        "Context Kernel Agent",
        "Token-frugal task runner for long-lived AI workspaces.",
    )
    chat_panel(
        "Session",
        [
            ("cwd", compact_path(Path.cwd())),
            ("workspace", compact_path(workspace.root)),
            ("task", task_id),
            ("provider", args.provider),
            ("primary", model),
            ("auxiliary", auxiliary_model(args)),
            ("routing", args.model_routing),
            ("review", args.aux_review),
            ("profile", args.profile),
            ("loop", f"max {args.max_steps} steps per message"),
            ("state", workspace_state_summary(workspace)),
        ],
    )
    if args.provider == "openai" and (not api_key_set or not base_url):
        chat_notice("Setup needed", "Run `akernel setup` before sending OpenAI-backed tasks.")
    chat_panel(
        "Start",
        [
            ("type", "Describe a task in natural language and press Enter."),
            ("include", "@path attaches a workspace file; !cmd runs a policy-checked command."),
            ("compose", "/paste captures a multi-line task; /compact shows the task brief."),
            ("inspect", "/status, /model, /task, /runs, /cost"),
            ("control", "/help, /config, /clear, /exit"),
        ],
    )


def resolve_chat_ui(args: argparse.Namespace) -> str:
    requested = getattr(args, "ui", "auto")
    if requested in {"classic", "tui"}:
        return requested
    if os.environ.get("AKERNEL_UI"):
        value = os.environ["AKERNEL_UI"].strip().lower()
        if value in {"classic", "tui"}:
            return value
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return "classic"
    if os.environ.get("TERM", "").lower() == "dumb":
        return "classic"
    return "tui"


def run_chat_loop_tui(
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    args: argparse.Namespace,
) -> None:
    last_report: dict[str, Any] | None = None
    pending_context: list[str] = []
    transcript: list[dict[str, str]] = [
        {
            "role": "system",
            "title": "Welcome",
            "text": "Describe a task, attach files with @path, run safe commands with !command, or type /help.",
        }
    ]
    use_alt_screen = sys.stdout.isatty() and not os.environ.get("AKERNEL_NO_ALT_SCREEN")
    if use_alt_screen:
        print("\033[?1049h", end="")
    try:
        render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="ready")
        while True:
            try:
                request = input(tui_prompt(args)).strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                transcript.append({"role": "system", "title": "Interrupted", "text": "Keyboard interrupt received."})
                render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="interrupted")
                break
            if not request:
                render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="ready")
                continue
            lowered = request.lower()
            if lowered in {"/exit", "/quit", "exit", "quit"}:
                break
            if lowered == "/clear":
                transcript.clear()
                render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="cleared")
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
                render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="ready")
                continue

            request_for_agent = merge_pending_context(request, pending_context)
            pending_context.clear()
            transcript.append({"role": "user", "title": "You", "text": request_for_agent})
            render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="running")
            last_report = AgentLoop(workspace).run(
                request_for_agent,
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
            )
            transcript.append({"role": "assistant", "title": "Assistant", "text": format_tui_report(last_report)})
            render_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status="ready")
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
    if lowered == "/help":
        transcript.append({"role": "system", "title": "Help", "text": format_chat_help_text()})
        return True
    if lowered == "/compact":
        transcript.append({"role": "system", "title": "Compact Brief", "text": capture_chat_output(lambda: print_task_brief_panel(tasks, task_id))})
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
            report = AgentLoop(workspace).run(
                request_for_agent,
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
            )
            state["last_report"] = report
            transcript.append({"role": "assistant", "title": "Assistant", "text": format_tui_report(report)})
        return True
    if request.startswith("@"):
        text = capture_chat_output(lambda: attach_chat_file(workspace, tasks, task_id, request[1:].strip(), pending_context))
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


def format_chat_help_text() -> str:
    rows = [
        ("/help", "show this command palette"),
        ("/status", "show workspace and runtime status"),
        ("/model", "show primary and auxiliary model roles"),
        ("/config", "show setup and environment guidance"),
        ("/compact", "show the compact task brief used for resume context"),
        ("/paste", "enter a multi-line task; finish with /end"),
        ("@path", "attach a workspace file to the next task"),
        ("!command", "run a policy-checked command and attach its summary"),
        ("/task", "print the current task session JSON"),
        ("/runs", "list recent agent runs"),
        ("/cost", "print the last agent run cost report"),
        ("/clear", "clear the transcript"),
        ("/exit", "leave the interactive session"),
    ]
    return "\n".join(f"{name:<10} {description}" for name, description in rows)


def tui_prompt(args: argparse.Namespace) -> str:
    return chat_color("\nakernel", "cyan", bold=True) + chat_color(f" [{primary_model(args)}]", "dim") + "> "


def render_chat_tui_screen(
    workspace: Workspace,
    task_id: str,
    args: argparse.Namespace,
    transcript: list[dict[str, str]],
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    *,
    status: str,
) -> None:
    screen = build_chat_tui_screen(workspace, task_id, args, transcript, last_report, pending_context, status=status)
    print("\033[2J\033[H" + screen, end="")


def build_chat_tui_screen(
    workspace: Workspace,
    task_id: str,
    args: argparse.Namespace,
    transcript: list[dict[str, str]],
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    *,
    status: str,
) -> str:
    width = chat_width()
    height = max(24, shutil.get_terminal_size((width, 32)).lines)
    right_width = min(40, max(32, width // 3))
    left_width = max(46, width - right_width - 3)
    header = tui_header_lines(workspace, args, last_report, status=status, width=width)
    footer = tui_footer_lines(width)
    body_height = max(10, height - len(header) - len(footer) - 1)
    lines = header
    body = tui_body_lines(transcript, left_width, status=status)
    side = tui_sidebar_lines(workspace, task_id, args, last_report, pending_context, right_width)
    body = body[-body_height:]
    side = side[:body_height]
    for index in range(body_height):
        left = body[index] if index < len(body) else ""
        right = side[index] if index < len(side) else ""
        lines.append(f"{left:<{left_width}} {chat_color('|', 'dim')} {right:<{right_width}}")
    lines.extend(footer)
    return "\n".join(lines)


def tui_header_lines(
    workspace: Workspace,
    args: argparse.Namespace,
    last_report: dict[str, Any] | None,
    *,
    status: str,
    width: int,
) -> list[str]:
    status_label = status.upper()
    status_color = "green" if status == "ready" else "yellow" if status == "running" else "cyan"
    tokens = 0 if not last_report else last_report.get("totals", {}).get("total_tokens", 0)
    title = f" Context Kernel TUI // AKERNEL // {status_label} "
    subtitle = (
        f"{compact_path(workspace.root)} | provider={args.provider} | "
        f"primary={primary_model(args)} | aux={auxiliary_model(args)} | last_tokens={tokens}"
    )
    return [
        chat_color(tui_rule(title, width), status_color, bold=True),
        truncate_line(subtitle, width),
        tui_command_strip(width),
    ]


def tui_footer_lines(width: int) -> list[str]:
    return [
        tui_rule(" Input ", width),
        truncate_line("Type a task. Use /help for palette, /compact for resume brief, @path for files, !command for checked shell, /exit to quit.", width),
        "",
    ]


def tui_command_strip(width: int) -> str:
    commands = " /help  /status  /model  /compact  /runs  /cost  @file  !cmd "
    return chat_color(truncate_line(commands.center(width, "-"), width), "dim")


def tui_body_lines(transcript: list[dict[str, str]], width: int, *, status: str = "ready") -> list[str]:
    if not transcript:
        return ["No messages yet. Start with one concrete task."]
    lines: list[str] = []
    lines.append(f"Transcript [{status}]")
    lines.append("-" * min(width, 22))
    for item in transcript:
        title = item.get("title", item.get("role", "message"))
        role = item.get("role", "system")
        label = tui_role_label(role, title)
        lines.append("")
        lines.append(truncate_line(f"+-- {label} " + "-" * max(0, width - len(label) - 5), width))
        prefix = "| " if role != "user" else "> "
        for line in wrap_plain(item.get("text", ""), width=max(20, width - len(prefix))).splitlines():
            lines.append(truncate_line(prefix + line, width))
        lines.append("")
    return lines


def tui_role_label(role: str, title: str) -> str:
    labels = {
        "user": "YOU",
        "assistant": "AGENT",
        "system": "SYSTEM",
    }
    base = labels.get(role, role.upper())
    return f"{base}: {title}" if title and title.casefold() != base.casefold() else base


def tui_sidebar_lines(
    workspace: Workspace,
    task_id: str,
    args: argparse.Namespace,
    last_report: dict[str, Any] | None,
    pending_context: list[str],
    width: int,
) -> list[str]:
    rows = tui_section("Cockpit", width)
    rows.extend(
        [
            f"provider: {args.provider}",
            f"profile:  {getattr(args, 'profile', DEFAULT_PROFILE)}",
            f"routing:  {getattr(args, 'model_routing', 'auto')}",
            f"steps:    {getattr(args, 'max_steps', '?')}",
            f"pending:  {len(pending_context)}",
            "",
        ]
    )
    rows.extend(tui_section("Model Stack", width))
    rows.extend(
        [
            f"primary:   {primary_model(args)}",
            f"auxiliary: {auxiliary_model(args)}",
            f"review:    {getattr(args, 'aux_review', 'auto')}",
            "",
        ]
    )
    rows.extend(tui_task_panel(workspace, task_id, width))
    if last_report:
        rows.extend(tui_last_run_panel(last_report, width))
        diagnostic = last_report.get("diagnostic")
        if isinstance(diagnostic, dict) and diagnostic:
            rows.extend([""])
            rows.extend(tui_section("Diagnostic", width))
            rows.append(str(diagnostic.get("category", "")))
            rows.extend(wrap_plain(str(diagnostic.get("suggestion", "")), width=max(20, width)).splitlines())
        rows.append("")
    rows.extend(tui_section("Workspace", width))
    rows.extend(wrap_plain(workspace_state_summary(workspace), width=max(20, width)).splitlines())
    return [truncate_line(line, width) for line in rows]


def tui_section(title: str, width: int) -> list[str]:
    label = f"[ {title} ]"
    return [label, "-" * min(width, len(label) + 6)]


def tui_task_panel(workspace: Workspace, task_id: str, width: int) -> list[str]:
    rows = tui_section("Mission", width)
    try:
        task = TaskStore(workspace).get(task_id)
    except (KeyError, FileNotFoundError):
        rows.extend([f"task      {task_id}", "status    unknown", ""])
        return rows
    rows.extend(
        [
            f"task:   {task.get('id', task_id)}",
            f"status: {task.get('status', 'unknown')}",
            f"title:  {truncate_line(str(task.get('title', '')), max(10, width - 10))}",
        ]
    )
    plan = task.get("plan")
    if isinstance(plan, dict):
        progress = plan.get("milestones", [])
        completed = sum(1 for item in progress if item.get("status") == "completed")
        active = next((item for item in progress if item.get("status") == "active"), None)
        rows.append(f"plan:   {completed}/{len(progress)} done")
        if active:
            rows.append(f"active: {active.get('id')} {truncate_line(str(active.get('title', '')), max(8, width - 13))}")
    rows.append("")
    return rows


def tui_last_run_panel(report: dict[str, Any], width: int) -> list[str]:
    rows = tui_section("Last Run Timeline", width)
    rows.extend(
        [
            f"id:     {report.get('id')}",
            f"status: {report.get('status')}",
            f"tokens: {report.get('totals', {}).get('total_tokens', 0)}",
        ]
    )
    steps = report.get("steps", [])
    if steps:
        compact_actions = " -> ".join(str((step.get("action") or {}).get("action") or "none") for step in steps)
        rows.append(f"actions: {truncate_line(compact_actions, max(10, width - 9))}")
        for step in steps[:4]:
            action = str((step.get("action") or {}).get("action") or "none")
            ok = "ok" if step.get("verifier_ok", True) else "check"
            rows.append(f"  {step.get('index', '?')}. {action} [{ok}]")
        if len(steps) > 4:
            rows.append(f"  ... +{len(steps) - 4} more")
    return rows


def format_tui_report(report: dict[str, Any]) -> str:
    actions = " -> ".join(str((step.get("action") or {}).get("action") or "none") for step in report.get("steps", []))
    parts = [
        f"status: {report.get('status')}",
        f"agent_run: {report.get('id')}",
        f"steps: {len(report.get('steps', []))}/{report.get('max_steps')}",
        f"tokens: {report.get('totals', {}).get('total_tokens', 0)}",
    ]
    if actions:
        parts.append(f"actions: {actions}")
    diagnostic = report.get("diagnostic")
    if isinstance(diagnostic, dict) and diagnostic:
        parts.append(f"diagnostic: {diagnostic.get('category')}")
        parts.append(f"next: {diagnostic.get('suggestion')}")
    if report.get("final_response"):
        parts.append("")
        parts.append(str(report["final_response"]))
    return "\n".join(parts)


def tui_rule(title: str, width: int) -> str:
    text = title[:width]
    remaining = max(0, width - len(text))
    left = remaining // 2
    right = remaining - left
    return "=" * left + text + "=" * right


def wrap_plain(text: str, *, width: int) -> str:
    return "\n".join(wrap_chat_text(text, width=width).splitlines())


def truncate_line(text: str, width: int) -> str:
    value = str(text)
    return value if len(value) <= width else value[: max(0, width - 3)] + "..."


def print_chat_turn_start(request: str, args: argparse.Namespace) -> None:
    preview = request if len(request) <= chat_width() - 14 else request[: chat_width() - 17] + "..."
    print("")
    print(chat_rule("New Task"))
    print(chat_color(f"you      {preview}", "bold"))
    print(chat_color("agent    building minimal context -> planning -> running bounded loop", "dim"))
    print(chat_color(f"runtime  provider={args.provider} max_steps={args.max_steps}", "dim"))


def print_chat_report(report: dict[str, Any]) -> None:
    actions = [
        str((step.get("action") or {}).get("action") or "none")
        for step in report.get("steps", [])
    ]
    print("")
    print(chat_rule("Result"))
    chat_panel(
        "Run Summary",
        [
            ("status", str(report["status"])),
            ("steps", f"{len(report['steps'])}/{report['max_steps']}"),
            ("tokens", str(report["totals"]["total_tokens"])),
            ("agent_run:", str(report["id"])),
        ],
    )
    if actions:
        print(chat_color("Actions", "cyan"))
        print(wrap_chat_text(" -> ".join(actions), indent="  "))
    print(chat_color("Models", "cyan"))
    print(wrap_chat_text(model_routing_summary(report), indent="  "))
    review_text = aux_review_summary(report)
    if review_text:
        print(chat_color("Review", "cyan"))
        print(wrap_chat_text(review_text, indent="  "))
    if report.get("state", {}).get("enabled"):
        print(chat_color(f"Memory  wrote {report['state']['written_count']} record(s)", "dim"))
    if report.get("final_response"):
        print("")
        print(chat_color("Assistant", "green", bold=True))
        print(wrap_chat_text(str(report["final_response"]), indent="  "))
    print("")
    print(chat_color(f"Next    /cost for cost report | akernel agent show {report['id']} for trace", "dim"))


def print_chat_help() -> None:
    chat_panel(
        "Command Palette",
        [
            ("/help", "show this command palette"),
            ("/status", "show workspace and runtime status"),
            ("/model", "show primary and auxiliary model roles"),
            ("/config", "show setup and environment guidance"),
            ("/compact", "show the compact task brief used for resume context"),
            ("/paste", "enter a multi-line task; finish with /end"),
            ("@path", "attach a workspace file to the next task"),
            ("!command", "run a policy-checked command and attach its summary"),
            ("/task", "print the current task session JSON"),
            ("/runs", "list recent agent runs"),
            ("/cost", "print the last agent run cost report"),
            ("/clear", "clear and redraw the session header"),
            ("/exit", "leave the interactive session"),
        ],
    )
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


def attach_chat_file(
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    path: str,
    pending_context: list[str],
) -> None:
    if not path:
        chat_notice("Attach File", "Usage: @relative/path.txt")
        return
    result = ToolExecutor(workspace).read_file(path)
    tasks.attach(task_id, "tool", result["id"])
    summary = summarize_tool_result(result)
    tasks.step(
        task_id,
        f"User attached file {path}: {summary}",
        kind="chat_file",
        refs={"tool_traces": [result["id"]]},
    )
    if result["ok"] and not result["blocked"]:
        output = result.get("output", {})
        content = str(output.get("content", ""))
        pending_context.append(
            f"Attached file `{path}` ({output.get('size_chars', len(content))} chars, "
            f"truncated={bool(output.get('truncated'))}):\n{content}"
        )
        chat_notice("Attached File", f"{path} is attached to the next task.")
    else:
        chat_notice("Attach Failed", summary)


def run_chat_command(
    workspace: Workspace,
    tasks: TaskStore,
    task_id: str,
    command: str,
    pending_context: list[str],
) -> None:
    if not command:
        chat_notice("Command", "Usage: !python -c \"print(123)\"")
        return
    result = ToolExecutor(workspace).run_command(command)
    tasks.attach(task_id, "tool", result["id"])
    summary = summarize_tool_result(result)
    tasks.step(
        task_id,
        f"User ran command `{command}`: {summary}",
        kind="chat_command",
        refs={"tool_traces": [result["id"]]},
    )
    output = result.get("output", {})
    pending_context.append(
        "Command result attached to the next task:\n"
        f"command: {command}\n"
        f"ok: {result.get('ok')}\n"
        f"blocked: {result.get('blocked')}\n"
        f"summary: {summary}\n"
        f"stdout: {str(output.get('stdout', ''))[:1200]}\n"
        f"stderr: {str(output.get('stderr', ''))[:800]}"
    )
    title = "Command Complete" if result.get("ok") else "Command Blocked" if result.get("blocked") else "Command Failed"
    chat_notice(title, summary)


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
        ("estimated_tokens", str(brief.get("estimated_tokens", 0))),
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
            ("routing", "auto can delegate low/medium first-step planning to auxiliary"),
            ("review_role", "auxiliary reviews primary-model steps when enabled"),
            ("mode", args.model_routing),
            ("review", args.aux_review),
            ("base_url", args.base_url or env_value("CONTEXT_KERNEL_OPENAI_BASE_URL") or "default"),
        ],
    )


def print_status_panel(workspace: Workspace, task_id: str, args: argparse.Namespace) -> None:
    chat_panel(
        "Status",
        [
            ("cwd", compact_path(Path.cwd())),
            ("workspace", compact_path(workspace.root)),
            ("task", task_id),
            ("provider", args.provider),
            ("primary", primary_model(args)),
            ("auxiliary", auxiliary_model(args)),
            ("routing", args.model_routing),
            ("review", args.aux_review),
            ("profile", args.profile),
            ("state", workspace_state_summary(workspace)),
        ],
    )


def print_config_panel() -> None:
    chat_panel(
        "Config",
        [
            ("setup", "akernel setup"),
            ("env", "CONTEXT_KERNEL_OPENAI_API_KEY, CONTEXT_KERNEL_OPENAI_BASE_URL"),
            ("models", "CONTEXT_KERNEL_OPENAI_MODEL, CONTEXT_KERNEL_OPENAI_AUX_MODEL"),
            ("scope", "current project .env first, installed Context Kernel .env fallback"),
        ],
    )


def chat_prompt(args: argparse.Namespace) -> str:
    model = primary_model(args)
    return "\n" + chat_color("akernel", "cyan", bold=True) + chat_color(f" [{model}]", "dim") + "> "


def primary_model(args: argparse.Namespace) -> str:
    return args.model or env_value("CONTEXT_KERNEL_OPENAI_MODEL") or DEFAULT_PRIMARY_MODEL


def auxiliary_model(args: argparse.Namespace) -> str:
    return getattr(args, "aux_model", None) or env_value("CONTEXT_KERNEL_OPENAI_AUX_MODEL") or DEFAULT_AUXILIARY_MODEL


def model_routing_summary(report: dict[str, Any]) -> str:
    parts = []
    for step in report.get("steps", []):
        role = step.get("model_role") or "primary"
        model = step.get("model") or "default"
        reason = step.get("routing_reason") or ""
        label = f"step {step.get('index')}: {role} ({model})"
        parts.append(f"{label} - {reason}" if reason else label)
    if not parts:
        routing = report.get("model_routing", {})
        return f"{routing.get('mode', 'auto')}: no provider step was executed"
    return "; ".join(parts)


def aux_review_summary(report: dict[str, Any]) -> str:
    parts = []
    for step in report.get("steps", []):
        review = step.get("aux_review", {})
        if not isinstance(review, dict) or not review.get("enabled"):
            continue
        parts.append(
            f"step {step.get('index')}: {review.get('risk')} risk, "
            f"{review.get('recommendation')} via {review.get('model')} "
            f"({review.get('tokens', {}).get('total_tokens', 0)}t)"
        )
    return "; ".join(parts)


def clear_chat_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")
    else:
        print("\n" * 30)


def chat_width() -> int:
    return max(88, min(shutil.get_terminal_size((112, 20)).columns, 132))


def chat_color(text: str, color: str, *, bold: bool = False) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    codes = {
        "cyan": "36",
        "green": "32",
        "yellow": "33",
        "red": "31",
        "dim": "2",
        "bold": "1",
    }
    selected: list[str] = []
    if bold:
        selected.append("1")
    selected.append(codes.get(color, "0"))
    return f"\033[{';'.join(selected)}m{text}\033[0m"


def chat_banner(title: str, subtitle: str) -> None:
    width = chat_width()
    print("")
    print(chat_color("=" * width, "cyan", bold=True))
    print(chat_color(title, "cyan", bold=True))
    print(chat_color(subtitle, "dim"))
    print(chat_color("=" * width, "cyan", bold=True))


def chat_rule(title: str) -> str:
    width = chat_width()
    label = f" {title} "
    remaining = max(0, width - len(label))
    left = remaining // 2
    right = remaining - left
    return chat_color("-" * left + label + "-" * right, "cyan")


def chat_panel(title: str, rows: list[tuple[str, str]]) -> None:
    width = chat_width()
    print("")
    print(chat_color(f"[ {title} ]", "cyan", bold=True))
    key_width = max(len(key) for key, _ in rows)
    for key, value in rows:
        prefix = f"  {key:<{key_width}}  "
        wrapped = wrap_chat_text(str(value), indent=" " * len(prefix), width=width)
        lines = wrapped.splitlines() or [""]
        print(chat_color(prefix, "dim") + lines[0].lstrip())
        for line in lines[1:]:
            print(line)


def chat_notice(title: str, message: str) -> None:
    print("")
    print(chat_color(f"! {title}", "yellow", bold=True))
    print(wrap_chat_text(message, indent="  "))


def wrap_chat_text(text: str, *, indent: str = "", width: int | None = None) -> str:
    width = width or chat_width()
    usable = max(30, width - len(indent))
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append(indent.rstrip())
            continue
        current = words[0]
        for word in words[1:]:
            if len(current) + 1 + len(word) > usable:
                lines.append(indent + current)
                current = word
            else:
                current += " " + word
        lines.append(indent + current)
    return "\n".join(lines)


def compact_path(path: Path) -> str:
    text = str(path)
    width = chat_width() - 18
    if len(text) <= width:
        return text
    return "..." + text[-max(12, width - 3) :]


def workspace_state_summary(workspace: Workspace) -> str:
    skills = len(list(workspace.skills_dir.glob("*.json"))) if workspace.skills_dir.exists() else 0
    runs = len(list(workspace.agent_runs_dir.glob("*.json"))) if workspace.agent_runs_dir.exists() else 0
    project = 1 if workspace.project_file.exists() else 0
    try:
        memories = len(MemoryStore(workspace).all())
    except Exception:
        memories = 0
    return f"{skills} skills, {memories} memories, {runs} runs, {project} project profiles"


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


def print_agent_report(report: dict[str, Any]) -> None:
    print(f"agent_run: {report['id']}")
    print(f"task: {report['task_id']}")
    print(f"status: {report['status']}")
    print(f"steps: {len(report['steps'])}/{report['max_steps']}")
    print(f"tokens: total={report['totals']['total_tokens']} input={report['totals']['input_tokens']} output={report['totals']['output_tokens']}")
    routing = report.get("model_routing", {})
    if routing:
        print(
            "model_routing: "
            f"mode={routing.get('mode')} "
            f"primary={routing.get('primary_model')} "
            f"auxiliary={routing.get('auxiliary_model')} "
            f"review={routing.get('aux_review')}"
        )
    if report.get("state", {}).get("enabled"):
        print(f"state: wrote {report['state']['written_count']} memory record(s)")
    print_agent_diagnostic(report.get("diagnostic"))
    for step in report["steps"]:
        trace = step["trace_id"] or "none"
        tokens = step.get("tokens", {}).get("total_tokens", 0)
        action = (step.get("action") or {}).get("action", "none")
        model_part = f" model={step.get('model_role')}:{step.get('model') or 'default'}" if step.get("model_role") else ""
        review = step.get("aux_review", {})
        review_part = ""
        if isinstance(review, dict) and review.get("enabled"):
            review_part = f" review={review.get('risk')}:{review.get('recommendation')}"
        tool = step.get("tool", {})
        tool_part = f" tool={tool.get('name')}:{tool.get('id')}" if tool else ""
        print(f"- step {step['index']}: {step['status']} action={action} trace={trace} tokens={tokens}{model_part}{review_part}{tool_part}")
        print_agent_diagnostic(step.get("diagnostic"), prefix="  ")
    if report.get("final_response"):
        print("")
        print(report["final_response"])


def print_agent_diagnostic(diagnostic: Any, *, prefix: str = "") -> None:
    if not isinstance(diagnostic, dict) or not diagnostic:
        return
    category = diagnostic.get("category") or "unknown"
    message = diagnostic.get("message") or ""
    suggestion = diagnostic.get("suggestion") or ""
    print(f"{prefix}diagnostic: {category}")
    if message:
        print(f"{prefix}reason: {message}")
    if suggestion:
        print(f"{prefix}next: {suggestion}")


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
    base_url = env_value("CONTEXT_KERNEL_OPENAI_BASE_URL")
    api_key = env_value("CONTEXT_KERNEL_OPENAI_API_KEY")
    model = env_value("CONTEXT_KERNEL_OPENAI_MODEL") or DEFAULT_PRIMARY_MODEL
    aux_model = env_value("CONTEXT_KERNEL_OPENAI_AUX_MODEL") or DEFAULT_AUXILIARY_MODEL
    print(f"project_root: {Path.cwd().resolve()}")
    print(f"workspace: {workspace.root}")
    print(f"workspace_initialized: {workspace.state.exists()}")
    print(f"workspace_config: {workspace.config_file}")
    print(f"workspace_config_version: {config.get('version')}")
    print(f"project_env_api_key_set: {bool(api_key)}")
    print(f"project_env_base_url: {normalize_openai_base_url(base_url or '') if base_url else ''}")
    print(f"project_env_primary_model: {model}")
    print(f"project_env_auxiliary_model: {aux_model}")
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


def cmd_tool_read(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).read_file(args.path, max_chars=args.max_chars)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)
    if result["ok"]:
        print(result["output"]["content"])


def cmd_tool_write(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).write_file(args.path, args.text)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)


def cmd_tool_patch(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    if (args.start_anchor or args.end_anchor) and args.old:
        raise ValueError("tool patch cannot combine --old with --start-anchor/--end-anchor")
    if not (args.start_anchor or args.end_anchor) and not args.old:
        raise ValueError("tool patch requires --old, or both --start-anchor and --end-anchor")
    result = ToolExecutor(workspace).patch_file(
        args.path,
        args.old or "",
        args.new,
        replace_all=args.replace_all,
        occurrence=args.occurrence,
        start_anchor=args.start_anchor,
        end_anchor=args.end_anchor,
        include_anchors=args.include_anchors,
    )
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)


def cmd_tool_batch_patch(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    edits = load_batch_patch_specs(Path(args.specs_file))
    result = ToolExecutor(workspace).batch_patch(edits)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)
    output = result.get("output", {})
    if "applied_count" in output:
        print(f"applied_count: {output['applied_count']}")
    if output.get("rolled_back"):
        print("rolled_back: true")


def load_batch_patch_specs(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    edits = payload.get("edits") if isinstance(payload, dict) else payload
    if not isinstance(edits, list):
        raise ValueError("batch-patch specs file must contain a JSON array or an object with an `edits` array.")
    if not all(isinstance(edit, dict) for edit in edits):
        raise ValueError("batch-patch edits must be JSON objects.")
    return edits


def cmd_tool_delete(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    result = ToolExecutor(workspace).delete_file(args.path, allow_destructive=args.allow_destructive)
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)


def cmd_tool_exec(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    ensure_task_attachable(workspace, args.task)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    result = ToolExecutor(workspace).run_command(
        " ".join(command),
        allow_destructive=args.allow_destructive,
        timeout_seconds=args.timeout,
    )
    attach_tool_to_task_if_requested(workspace, args.task, result)
    if args.json:
        print_json(result)
        return
    print_tool_result(result)
    output = result.get("output", {})
    if "exit_code" in output:
        print(f"exit_code: {output['exit_code']}")
    if output.get("stdout"):
        print("stdout:")
        print(output["stdout"].rstrip())
    if output.get("stderr"):
        print("stderr:")
        print(output["stderr"].rstrip())


def cmd_tool_list(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    traces = ToolExecutor(workspace).list_traces()
    if not traces:
        print("no tool traces")
        return
    for trace in traces:
        status = "blocked" if trace["blocked"] else "ok" if trace["ok"] else "failed"
        print(f"{trace['id']}\t{trace['created_at']}\t{trace['tool']}\t{status}\t{trace['subject']}")


def cmd_tool_show(args: argparse.Namespace) -> None:
    workspace = workspace_from_args(args)
    print_json(ToolExecutor(workspace).get_trace(args.trace_id))


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


def print_policy_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print_json(result)
        return
    print(f"{result['status']}: {result['kind']} {result['operation']} {result['subject']}")
    for reason in result["reasons"]:
        print(f"- {reason}")


def print_tool_result(result: dict[str, Any]) -> None:
    status = "blocked" if result["blocked"] else "ok" if result["ok"] else "failed"
    print(f"{status}: {result['tool']} trace={result['id']}")
    if result.get("error"):
        print(f"error: {result['error']}")
    print(f"policy: {result['policy']['status']}")


def list_agent_reports(workspace: Workspace) -> list[dict[str, Any]]:
    workspace.agent_runs_dir.mkdir(parents=True, exist_ok=True)
    reports = [Workspace.read_json(path) for path in sorted(workspace.agent_runs_dir.glob("*.json"))]
    return sorted(reports, key=lambda report: report.get("created_at", ""), reverse=True)


def print_benchmark_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"report: {report['id']}")
    print(f"benchmark: {report['benchmark']}")
    print(f"fixtures: {summary['fixture_count']}")
    print(f"tasks: {summary['task_count']}")
    print(f"avg_savings: {summary['average_savings_percent']}%")
    print(f"total_kernel_tokens: {summary['total_kernel_tokens']}")
    print(f"total_baseline_tokens: {summary['total_baseline_tokens']}")
    if summary["executed_tasks"]:
        print(f"executed_tasks: {summary['executed_tasks']}")
        print(f"execution_tokens: {summary['total_execution_tokens']}")
    if summary.get("blocked_tasks"):
        print(f"blocked_tasks: {summary['blocked_tasks']}")
    print(f"checks: {summary['passed_checks']}/{summary['total_checks']}")
    for fixture in report["fixtures"]:
        fixture_summary = fixture["summary"]
        print(
            f"{Path(fixture['fixture']).name}\t"
            f"tasks={fixture_summary['task_count']}\t"
            f"avg_savings={fixture_summary['average_savings_percent']}%\t"
            f"checks={fixture_summary['passed_checks']}/{fixture_summary['total_checks']}"
        )


def print_benchmark_check_summary(report: dict[str, Any]) -> None:
    summary = report.get("summary", {})
    print(f"current_checks: {summary.get('passed_checks', 0)}/{summary.get('total_checks', 0)}")


def benchmark_report_ok(report: dict[str, Any]) -> bool:
    return bool(report.get("summary", {}).get("ok", False))


def enforce_benchmark_report_gate(report: dict[str, Any], *, label: str) -> None:
    if benchmark_report_ok(report):
        return
    summary = report.get("summary", {})
    passed = summary.get("passed_checks", 0)
    total = summary.get("total_checks", 0)
    raise RuntimeError(f"{label} current benchmark checks failed: {passed}/{total}")


def print_benchmark_diff(diff: dict[str, Any]) -> None:
    summary = diff["summary_delta"]
    print(f"before: {diff['before']['id']}")
    print(f"after: {diff['after']['id']}")
    print(f"fixtures_delta: {summary['fixtures']}")
    print(f"tasks_delta: {summary['tasks']}")
    print(f"kernel_tokens_delta: {summary['kernel_tokens']}")
    print(f"baseline_tokens_delta: {summary['baseline_tokens']}")
    print(f"savings_tokens_delta: {summary['savings_tokens']}")
    print(f"savings_percent_delta: {summary['savings_percent']}")
    print(f"checks_delta: {summary['passed_checks']}/{summary['total_checks']}")
    print(f"execution_tokens_delta: {summary['execution_tokens']}")
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
    for fixture in diff["fixtures"]:
        if fixture["status"] != "changed":
            print(f"{fixture['fixture']}\t{fixture['status']}")
            continue
        fixture_summary = fixture["summary_delta"]
        print(
            f"{fixture['fixture']}\t"
            f"kernel_delta={fixture_summary['kernel_tokens']}\t"
            f"savings_delta={fixture_summary['savings_percent']}%\t"
            f"checks_delta={fixture_summary['passed_checks']}/{fixture_summary['total_checks']}\t"
            f"regressions={len(fixture['regressions'])}"
        )


def enforce_regression_gate(diff: dict[str, Any], *, enabled: bool, label: str) -> None:
    if not enabled or diff.get("ok", True):
        return
    reasons: list[str] = []
    regressions = diff.get("regressions", [])
    cost_regressions = diff.get("cost_regressions", [])
    if regressions:
        reasons.append(f"{len(regressions)} behavior regression(s)")
    if cost_regressions:
        reasons.append(f"{len(cost_regressions)} cost regression(s)")
    if not reasons:
        reasons.append("regressions detected")
    raise RuntimeError(f"{label} found regressions: {', '.join(reasons)}")


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
