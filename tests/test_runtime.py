from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

from context_kernel.agent_reports import build_agent_cost_report, render_agent_cost_report
from context_kernel.benchmarks import BenchmarkRunner, render_benchmark_evidence_markdown, render_benchmark_markdown
from context_kernel.budget import allocate_budget
from context_kernel.chat_commands import expand_custom_chat_command
from context_kernel.chat_tui import build_chat_tui_screen
from context_kernel.chat_ui import format_tui_report
from context_kernel.cli import (
    chat_completion_items,
    load_batch_patch_specs,
    load_transaction_specs,
    main,
    print_chat_report,
    run_agent_with_spinner,
    spinner_message_from_event,
)
from context_kernel.cli_reports import print_agent_report
from context_kernel.context import ContextBuilder
from context_kernel.evals import EvalRunner
from context_kernel.global_memory import pull_global_memories, push_global_memories
from context_kernel.loop import AgentLoop, parse_agent_action, repeated_agent_action
from context_kernel.loop_actions import execute_agent_action
from context_kernel.loop_execution import (
    build_agent_packet,
    parse_agent_step_action,
    response_token_counts,
)
from context_kernel.loop_planning import adaptive_context_budget
from context_kernel.loop_recovery import command_failure_target_paths, diagnose_agent_exception
from context_kernel.loop_routing import parse_aux_review, select_model_role
from context_kernel.loop_steps import agent_step_result
from context_kernel.marketplace import install_marketplace_skill, list_marketplace_skills
from context_kernel.memory import MemoryStore, is_relevant_memory_match
from context_kernel.mcp import add_mcp_server, import_codex_mcp_servers, list_mcp_servers, mcp_context_summary
from context_kernel.planner import ExecutionPlanner
from context_kernel.policy import assess_request_policy, check_command_policy, check_file_policy
from context_kernel.project import load_project_profile, scan_project
from context_kernel.providers import OpenAICompatibleProvider, ProviderResponse, build_messages, env_value, extract_text, normalize_openai_base_url, parse_env_file
from context_kernel.report_costs import build_benchmark_cost_report, build_eval_cost_report, diff_cost_reports, render_cost_report
from context_kernel.runner import AgentRunner
from context_kernel.state_writer import StateWriter, marker_candidates, redact
from context_kernel.skills import (
    SkillRegistry,
    compile_markdown_skill,
    extract_json_object,
    inspect_skill,
    validate_skill_file,
)
from context_kernel.storage import Workspace
from context_kernel.tasks import TaskStore
from context_kernel.tools import ToolExecutor
from context_kernel.verifier import verify_trace


ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def test_context_builder_selects_relevant_skill_and_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])

            packet = ContextBuilder(workspace).build("Plan a CLI context budget prototype", 900)

            self.assertFalse(packet["budget"]["over_budget"])
            self.assertEqual(packet["skills"][0]["contract"]["id"], "context_budget")
            self.assertEqual(packet["memory"][0]["record"]["kind"], "preference")
            self.assertIn("context", packet["skills"][0]["matched_terms"])

    def test_skill_fallback_is_only_used_when_nothing_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            registry = SkillRegistry(workspace)
            registry.register(ROOT / "examples" / "skills" / "edit_file.json")
            registry.register(ROOT / "examples" / "skills" / "context_budget.json")

            selected = registry.select("unrelated astronomy request", budget_tokens=300)

            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0].score, 0)
            self.assertEqual(selected[0].level, "l0")

    def test_compare_reports_savings_against_full_load_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            registry = SkillRegistry(workspace)
            registry.register(ROOT / "examples" / "skills" / "edit_file.json")
            registry.register(ROOT / "examples" / "skills" / "context_budget.json")
            memory = MemoryStore(workspace)
            memory.add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            memory.add("fact", "This project is a context-native agent runtime.", ["architecture"])

            comparison = ContextBuilder(workspace).compare("Plan a CLI context budget prototype", 900)

            self.assertLess(
                comparison["kernel"]["estimated_tokens"],
                comparison["baseline"]["estimated_tokens"],
            )
            self.assertGreater(comparison["savings"]["estimated_tokens"], 0)
            self.assertEqual(comparison["baseline"]["skill_level"], "l3")

    def test_budget_profiles_change_default_totals(self) -> None:
        lean = allocate_budget("Plan a CLI context budget prototype", profile="lean")
        deep = allocate_budget("Plan a CLI context budget prototype", profile="deep")

        self.assertEqual(lean.profile, "lean")
        self.assertEqual(deep.profile, "deep")
        self.assertLess(lean.total, deep.total)

    def test_project_scan_writes_profile_and_enters_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / "README.md").write_text("demo", encoding="utf-8")
            (root / "AGENTS.md").write_text("Always run the project test command before reporting success.", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_demo.py").write_text("def test_demo():\n    assert True\n", encoding="utf-8")
            workspace = Workspace(root)
            workspace.init()

            profile = scan_project(workspace)
            packet = ContextBuilder(workspace).build("Run tests", 1200)
            config = workspace.load_config()

            self.assertTrue(workspace.project_file.exists())
            self.assertEqual(load_project_profile(workspace)["version"], 1)
            self.assertIn("python", profile["languages"])
            self.assertEqual(profile["commands"]["test"], "python -m pytest")
            self.assertIn("README.md", profile["key_files"])
            self.assertEqual(profile["instructions"][0]["path"], "AGENTS.md")
            self.assertIn("project", packet["runtime"])
            self.assertIn("python", packet["runtime"]["project"]["languages"])
            self.assertIn("Always run", packet["runtime"]["project"]["instructions"][0]["content"])
            self.assertIn("python", config["command_policy"]["allowed_roots"])

    def test_mcp_server_config_enters_context_as_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            server = add_mcp_server(
                workspace,
                "filesystem",
                command="python -m mcp_server_filesystem .",
                tools=["read_file:Read workspace files", "list_dir:List directories"],
            )
            packet = ContextBuilder(workspace).build("Read files through MCP if needed", 1200)
            summary = mcp_context_summary(workspace)

            self.assertTrue(workspace.mcp_file.exists())
            self.assertEqual(server["name"], "filesystem")
            self.assertEqual(list_mcp_servers(workspace)[0]["tools"][0]["name"], "read_file")
            self.assertEqual(summary["enabled_count"], 1)
            self.assertIn("mcp", packet["runtime"])
            self.assertEqual(packet["runtime"]["mcp"]["servers"][0]["name"], "filesystem")
            self.assertEqual(packet["runtime"]["mcp"]["servers"][0]["command_root"], "python")

    def test_mcp_cli_manages_server_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "mcp",
                        "add",
                        "demo",
                        "--command",
                        "python server.py",
                        "--tool",
                        "search:Search project data",
                    ]
                )
            self.assertIn("mcp: demo", stdout.getvalue())

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "mcp", "list"])
            self.assertIn("demo", stdout.getvalue())
            self.assertIn("tools=1", stdout.getvalue())

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "mcp", "disable", "demo"])
            self.assertIn("disabled MCP server: demo", stdout.getvalue())
            self.assertEqual(mcp_context_summary(workspace)["enabled_count"], 0)

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "mcp", "enable", "demo"])
            self.assertIn("enabled MCP server: demo", stdout.getvalue())
            self.assertEqual(mcp_context_summary(workspace)["enabled_count"], 1)

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "mcp", "remove", "demo"])
            self.assertIn("removed MCP server: demo", stdout.getvalue())
            self.assertEqual(list_mcp_servers(workspace), [])

    def test_mcp_imports_codex_config_without_copying_env_values_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "codex.toml"
            config_path.write_text(
                "\n".join(
                    [
                        '[mcp_servers.gitnexus]',
                        'command = "node"',
                        'args = ["D:/GitNexus/dist/index.js", "mcp"]',
                        'startup_timeout_ms = 20000',
                        "",
                        '[mcp_servers.gitnexus.tools.query]',
                        'approval_mode = "approve"',
                        "",
                        '[mcp_servers.page-agent]',
                        'command = "node"',
                        'args = ["D:/page-agent/index.js"]',
                        "",
                        '[mcp_servers.page-agent.env]',
                        'LLM_API_KEY = "secret-value"',
                        'PORT = "38431"',
                    ]
                ),
                encoding="utf-8",
            )
            workspace = Workspace(root / "workspace")
            workspace.init()

            result = import_codex_mcp_servers(workspace, config_path=config_path)
            servers = {server["name"]: server for server in list_mcp_servers(workspace)}
            summary = mcp_context_summary(workspace)

            self.assertEqual(result["count"], 2)
            self.assertEqual(servers["gitnexus"]["command"], "node")
            self.assertEqual(servers["gitnexus"]["args"], ["D:/GitNexus/dist/index.js", "mcp"])
            self.assertEqual(servers["gitnexus"]["startup_timeout_ms"], 20000)
            self.assertEqual(servers["gitnexus"]["tool_approvals"]["query"]["approval_mode"], "approve")
            self.assertEqual(servers["page-agent"]["env"], {})
            self.assertEqual(servers["page-agent"]["env_keys"], ["LLM_API_KEY", "PORT"])
            self.assertFalse(servers["page-agent"]["enabled"])
            self.assertTrue(servers["page-agent"]["env_required"])
            self.assertEqual(summary["enabled_count"], 1)

    def test_mcp_import_codex_cli_can_copy_env_when_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "codex.toml"
            config_path.write_text(
                "\n".join(
                    [
                        '[mcp_servers.demo]',
                        'command = "python"',
                        'args = ["server.py"]',
                        "",
                        '[mcp_servers.demo.env]',
                        'TOKEN = "secret-value"',
                    ]
                ),
                encoding="utf-8",
            )
            workspace = Workspace(root / "workspace")
            workspace.init()

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "mcp",
                        "import-codex",
                        "--config",
                        str(config_path),
                        "--include-env",
                    ]
                )

            server = list_mcp_servers(workspace)[0]
            self.assertIn("imported MCP servers from Codex: 1", stdout.getvalue())
            self.assertEqual(server["env"], {"TOKEN": "secret-value"})
            self.assertEqual(server["env_keys"], ["TOKEN"])

    def test_mcp_refresh_discovers_stdio_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server_path = root / "fake_mcp_server.py"
            server_path.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "for line in sys.stdin:",
                        "    msg = json.loads(line)",
                        "    method = msg.get('method')",
                        "    if method == 'initialize':",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2024-11-05','serverInfo':{'name':'fake','version':'1.0'}}}), flush=True)",
                        "    elif method == 'tools/list':",
                        "        tools = [{'name':'search','description':'Search local test data'}, {'name':'read','description':'Read local test data'}]",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'tools':tools}}), flush=True)",
                    ]
                ),
                encoding="utf-8",
            )
            workspace = Workspace(root)
            workspace.init()
            command = f'"{sys.executable}" "{server_path}"'
            add_mcp_server(workspace, "fake", command=command)

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "mcp", "refresh", "fake", "--timeout", "5"])

            output = stdout.getvalue()
            server = list_mcp_servers(workspace)[0]
            packet = ContextBuilder(workspace).build("Use MCP search if needed", 1200)

            self.assertIn("refreshed MCP server: fake", output)
            self.assertIn("search", output)
            self.assertEqual([tool["name"] for tool in server["tools"]], ["search", "read"])
            self.assertEqual(packet["runtime"]["mcp"]["servers"][0]["tools"][0]["name"], "search")

    def test_mcp_call_invokes_stdio_tool_and_writes_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server_path = root / "fake_mcp_call_server.py"
            server_path.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "for line in sys.stdin:",
                        "    msg = json.loads(line)",
                        "    method = msg.get('method')",
                        "    if method == 'initialize':",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2024-11-05','serverInfo':{'name':'fake-call'}}}), flush=True)",
                        "    elif method == 'tools/list':",
                        "        tools = [{'name':'echo','description':'Echo text'}]",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'tools':tools}}), flush=True)",
                        "    elif method == 'tools/call':",
                        "        text = msg.get('params', {}).get('arguments', {}).get('text', '')",
                        "        result = {'content':[{'type':'text','text':'echo:' + text}]}",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)",
                    ]
                ),
                encoding="utf-8",
            )
            workspace = Workspace(root)
            workspace.init()
            add_mcp_server(workspace, "fake", command=f'"{sys.executable}" "{server_path}"')
            with patch("sys.stdout", new=io.StringIO()):
                main(["--workspace", str(workspace.root), "mcp", "refresh", "fake"])

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "mcp",
                        "call",
                        "fake",
                        "echo",
                        "--args",
                        '{"text":"hello"}',
                    ]
                )

            output = stdout.getvalue()
            traces = ToolExecutor(workspace).list_traces()
            trace = ToolExecutor(workspace).get_trace(traces[0]["id"])

            self.assertIn("mcp call: fake.echo", output)
            self.assertIn("echo:hello", output)
            self.assertEqual(trace["tool"], "mcp_call")
            self.assertEqual(trace["policy"]["subject"], "fake.echo")
            self.assertEqual(trace["output"]["result"]["content"][0]["text"], "echo:hello")

    def test_agent_loop_can_call_mcp_tool_then_respond(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server_path = root / "fake_agent_mcp_server.py"
            server_path.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "for line in sys.stdin:",
                        "    msg = json.loads(line)",
                        "    method = msg.get('method')",
                        "    if method == 'initialize':",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2024-11-05','serverInfo':{'name':'agent-mcp'}}}), flush=True)",
                        "    elif method == 'tools/list':",
                        "        tools = [{'name':'echo','description':'Echo text'}]",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'tools':tools}}), flush=True)",
                        "    elif method == 'tools/call':",
                        "        text = msg.get('params', {}).get('arguments', {}).get('text', '')",
                        "        result = {'content':[{'type':'text','text':'agent-echo:' + text}]}",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)",
                    ]
                ),
                encoding="utf-8",
            )
            workspace = Workspace(root)
            workspace.init()
            add_mcp_server(workspace, "fake", command=f'"{sys.executable}" "{server_path}"')
            with patch("sys.stdout", new=io.StringIO()):
                main(["--workspace", str(workspace.root), "mcp", "refresh", "fake"])

            report = AgentLoop(workspace).run(
                "Use MCP to echo hello.",
                provider_name="mock",
                budget=1800,
                max_steps=2,
                remember=False,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["mcp_call", "respond"])
            self.assertIn("agent-echo:Use MCP to echo hello.", report["final_response"])
            traces = ToolExecutor(workspace).list_traces()
            self.assertEqual(ToolExecutor(workspace).get_trace(traces[0]["id"])["tool"], "mcp_call")

    def test_project_scan_cli_outputs_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{"test":"node test.js","build":"node build.js"}}', encoding="utf-8")
            workspace = Workspace(root)
            workspace.init()

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(root), "project", "scan"])

            output = stdout.getvalue()
            self.assertTrue(workspace.project_file.exists())
            self.assertIn("languages: javascript/typescript", output)
            self.assertIn("command_test:", output)
            self.assertIn("instructions: none", output)
            self.assertIn("config_updated: True", output)

    def test_workspace_read_json_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.json"
            path.write_text('{"ok": true}', encoding="utf-8-sig")

            self.assertTrue(Workspace.read_json(path)["ok"])

    def test_npm_launcher_upgrades_stale_python_runtime(self) -> None:
        launcher = ROOT / "packages" / "npm" / "akernel" / "bin" / "akernel.js"
        text = launcher.read_text(encoding="utf-8")

        self.assertIn("context_kernel.__version__", text)
        self.assertIn("versionAtLeast", text)
        self.assertIn("akernel-runtime>=", text)

    def test_loop_guard_allows_one_duplicate_tool_step(self) -> None:
        action = {"action": "run_command", "command": "python -m pytest"}

        self.assertFalse(repeated_agent_action([{"action": action}], action))
        self.assertTrue(repeated_agent_action([{"action": action}, {"action": action}], action))

    def test_loop_module_boundary_helpers_keep_contracts(self) -> None:
        plan = {
            "route": {"complexity": "medium"},
            "budget": {"estimated_used": 5000, "total": 1200},
            "task": {"id": "task"},
            "selection": {"memory": ["m1"], "skills": ["s1", "s2"]},
            "warnings": [],
        }

        role, reason = select_model_role(
            model_routing="auto",
            plan=plan,
            step_index=1,
            prior_steps=[],
            profile="balanced",
        )
        self.assertEqual(role, "auxiliary")
        self.assertIn("medium-complexity", reason)
        self.assertEqual(adaptive_context_budget(plan), 6250)
        self.assertEqual(
            response_token_counts({"response": {"input_tokens": "3", "output_tokens": 4}})["total_tokens"],
            0,
        )

        step = agent_step_result(index=1, status="ok", can_continue=True, plan=plan, stop_reason="continue")
        self.assertEqual(step["plan"]["selection"]["skill_count"], 2)
        self.assertEqual(step["tokens"], {})

        review = parse_aux_review('{"ok": false, "risk": "wild", "recommendation": "bad", "notes": "single"}')
        self.assertFalse(review["ok"])
        self.assertEqual(review["risk"], "medium")
        self.assertEqual(review["recommendation"], "continue")
        self.assertEqual(review["notes"], ["single"])

        packet = build_agent_packet(
            "write demo.py and run command python -m unittest",
            1,
            2,
            expect_json=True,
            model_role="primary",
            routing_reason="test",
        )
        rules = packet["agent"]["response_contract"]["rules"]
        self.assertTrue(any("write the file first" in rule for rule in rules))
        self.assertEqual(
            packet["agent"]["available_tools"][0]["schema"]["message"],
            "string containing compact JSON text",
        )

    def test_loop_execution_recovers_contract_when_verifier_is_non_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            tasks = TaskStore(workspace)
            task = tasks.start("contract recovery")
            plan = {
                "route": {"complexity": "low"},
                "budget": {"estimated_used": 100, "total": 1200},
                "task": {"id": task["id"]},
                "selection": {"memory": [], "skills": []},
                "warnings": [],
            }
            trace = {
                "id": "run123",
                "response": {"text": '{"action":"respond","message":"ok"}'},
            }

            action, recovered, failure = parse_agent_step_action(
                tasks,
                task_id=task["id"],
                index=1,
                trace=trace,
                expect_json=False,
                verifier_ok=False,
                prior_steps=[],
                plan=plan,
                model_role="primary",
                model="model",
                routing_reason="test",
                aux_review={"enabled": False},
                tokens={"total_tokens": 1},
            )

            self.assertIsNone(failure)
            self.assertTrue(recovered)
            self.assertEqual(action["action"], "respond")
            saved = tasks.get(task["id"])
            self.assertIn("recovered a valid action", saved["steps"][-1]["note"])

    def test_loop_recovery_extracts_workspace_failure_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            src = Path(tmp) / "src" / "first.py"
            src.parent.mkdir()
            src.write_text("raise RuntimeError()\n", encoding="utf-8")
            tests_dir = Path(tmp) / "tests"
            tests_dir.mkdir()
            second = tests_dir / "second.py"
            second.write_text("assert False\n", encoding="utf-8")
            outside = Path(tmp).parent / "outside_failure.py"
            tool_result = {
                "output": {
                    "stderr": (
                        f'File "{src}", line 1, in <module>\n'
                        f"{outside}:2: outside\n"
                        f"{second}:3: failed\n"
                    )
                },
                "error": "",
            }

            targets = command_failure_target_paths(workspace, tool_result, limit=3)

            self.assertEqual(targets[:2], ["tests/second.py", "src/first.py"])
            diagnostic = diagnose_agent_exception(RuntimeError("Provider HTTP 404: missing model"))
            self.assertEqual(diagnostic["category"], "provider_endpoint")

    def test_agent_uses_project_profile_test_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            Workspace.write_json(
                workspace.project_file,
                {
                    "version": 1,
                    "summary": "languages=python; commands=test",
                    "languages": ["python"],
                    "package_managers": ["python/custom"],
                    "commands": {"test": "python -c \"print(42)\""},
                    "command_roots": ["python"],
                    "key_files": [],
                },
            )

            report = AgentLoop(workspace).run(
                "Run tests and tell me the result.",
                provider_name="mock",
                budget=1400,
                max_steps=2,
                remember=False,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["run_command", "respond"])
            self.assertEqual(report["steps"][0]["action"]["command"], 'python -c "print(42)"')
            self.assertIn("42", report["final_response"])

    def test_agent_can_fix_simple_failing_test_from_project_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            test_file = root / "tests" / "test_bug.py"
            test_file.write_text(
                "def answer():\n"
                "    return 1\n\n"
                "if answer() != 2:\n"
                "    raise AssertionError('assert 1 == 2')\n",
                encoding="utf-8",
            )
            workspace = Workspace(root)
            workspace.init()
            Workspace.write_json(
                workspace.project_file,
                {
                    "version": 1,
                    "summary": "simple failing test fixture",
                    "languages": ["python"],
                    "package_managers": ["python/custom"],
                    "commands": {"test": "python tests/test_bug.py"},
                    "command_roots": ["python"],
                    "key_files": ["tests/test_bug.py"],
                },
            )

            report = AgentLoop(workspace).run(
                "Fix the failing tests.",
                provider_name="mock",
                budget=2600,
                max_steps=4,
                remember=False,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual(
                [step["action"]["action"] for step in report["steps"]],
                ["run_command", "patch_file", "run_command", "respond"],
            )
            self.assertIn("return 2", test_file.read_text(encoding="utf-8"))
            self.assertIn("Project tests passed", report["final_response"])

    def test_agent_can_fix_simple_multi_file_failure_from_project_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            core_file = root / "src" / "core.py"
            util_file = root / "src" / "util.py"
            core_file.write_text("def core_answer():\n    return 1\n", encoding="utf-8")
            util_file.write_text("def util_answer():\n    return 1\n", encoding="utf-8")
            (root / "tests" / "fail_multi.py").write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "root = Path(__file__).resolve().parents[1]\n"
                "core = (root / 'src' / 'core.py').read_text(encoding='utf-8')\n"
                "util = (root / 'src' / 'util.py').read_text(encoding='utf-8')\n"
                "if 'return 2' not in core or 'return 2' not in util:\n"
                "    sys.stderr.write('src/core.py:1\\nsrc/util.py:1\\nAssertionError: assert 1 == 2\\n')\n"
                "    raise SystemExit(1)\n"
                "print('ok')\n",
                encoding="utf-8",
            )
            workspace = Workspace(root)
            workspace.init()
            Workspace.write_json(
                workspace.project_file,
                {
                    "version": 1,
                    "summary": "multi-file failing test fixture",
                    "languages": ["python"],
                    "package_managers": ["python/custom"],
                    "commands": {"test": "python tests/fail_multi.py"},
                    "command_roots": ["python"],
                    "key_files": ["src/core.py", "src/util.py", "tests/fail_multi.py"],
                },
            )

            report = AgentLoop(workspace).run(
                "Fix the failing tests.",
                provider_name="mock",
                budget=3200,
                max_steps=4,
                remember=False,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual(
                [step["action"]["action"] for step in report["steps"]],
                ["run_command", "batch_patch", "run_command", "respond"],
            )
            self.assertIn("return 2", core_file.read_text(encoding="utf-8"))
            self.assertIn("return 2", util_file.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(report["steps"][0].get("recovery_tools", [])), 2)
            self.assertIn("Project tests passed", report["final_response"])

    def test_agent_recovers_fenced_action_from_chatty_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes").mkdir()
            (root / "notes" / "plan.txt").write_text("ship small reliable loops", encoding="utf-8")
            workspace = Workspace(root)
            workspace.init()

            report = AgentLoop(workspace).run(
                "Read notes/plan.txt",
                provider_name="mock-chatty",
                budget=1600,
                max_steps=2,
                remember=False,
                aux_review="off",
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["read_file", "respond"])
            self.assertTrue(all(step["contract_recovered"] for step in report["steps"]))
            self.assertFalse(any(step["verifier_ok"] for step in report["steps"]))
            self.assertIn("ship small reliable loops", report["final_response"])

    def test_agent_reports_provider_configuration_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            with patch.object(
                AgentRunner,
                "run",
                side_effect=ValueError("Missing AKERNEL_OPENAI_API_KEY for OpenAI-compatible provider."),
            ):
                report = AgentLoop(workspace).run(
                    "Reply with OK.",
                    provider_name="openai",
                    budget=1200,
                    max_steps=1,
                    remember=False,
                    aux_review="off",
                )

            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["diagnostic"]["category"], "provider_configuration")
            self.assertIn("akernel setup", report["diagnostic"]["suggestion"])

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                print_agent_report(report)

            output = stdout.getvalue()
            self.assertIn("outcome: failed - Agent loop stopped: provider_configuration.", output)
            self.assertIn("diagnostic: provider_configuration", output)
            self.assertIn("next: Run `akernel setup`", output)
            self.assertIn("resume: fix provider setup", output)

    def test_eval_runner_reports_checks_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            registry = SkillRegistry(workspace)
            registry.register(ROOT / "examples" / "skills" / "edit_file.json")
            registry.register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])

            runner = EvalRunner(workspace)
            report = runner.run_fixture(ROOT / "examples" / "evals" / "phase2.json")

            self.assertEqual(report["summary"]["task_count"], 2)
            self.assertEqual(report["summary"]["passed_checks"], report["summary"]["total_checks"])
            self.assertGreater(report["summary"]["total_savings_tokens"], 0)
            self.assertTrue((workspace.evals_dir / f"{report['id']}.json").exists())
            self.assertEqual(runner.list_reports()[0]["id"], report["id"])
            self.assertEqual(runner.get_report(report["id"])["id"], report["id"])

    def test_eval_diff_reports_summary_and_task_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            registry = SkillRegistry(workspace)
            registry.register(ROOT / "examples" / "skills" / "edit_file.json")
            registry.register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = EvalRunner(workspace)
            before = runner.run_fixture(ROOT / "examples" / "evals" / "phase2.json")
            MemoryStore(workspace).add("fact", "Extra unrelated memory increases baseline context.", ["noise"])
            after = runner.run_fixture(ROOT / "examples" / "evals" / "phase2.json")

            diff = runner.diff_reports(before["id"], after["id"])

            self.assertIn("summary_delta", diff)
            self.assertIn("cost_diff", diff)
            self.assertEqual(len(diff["tasks"]), 2)
            self.assertGreater(diff["summary_delta"]["baseline_tokens"], 0)
            self.assertGreater(len(diff["cost_regressions"]), 0)
            self.assertTrue(any(item["kind"] == "weakest_savings_percent" for item in diff["cost_regressions"]))

    def test_eval_diff_can_fail_on_regression_for_cli_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = EvalRunner(workspace)
            before = runner.run_fixture(ROOT / "examples" / "evals" / "phase2.json")
            MemoryStore(workspace).add("fact", "Extra unrelated memory increases baseline context.", ["noise"])
            after = runner.run_fixture(ROOT / "examples" / "evals" / "phase2.json")

            with patch("sys.stdout", new=io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "eval",
                            "diff",
                            before["id"],
                            after["id"],
                            "--fail-on-regression",
                        ]
                    )

            self.assertIn("eval diff found regressions", str(exc.exception))

    def test_cost_diff_detects_hotspot_and_execution_regressions(self) -> None:
        before = {
            "kind": "eval",
            "id": "before",
            "name": "Before",
            "source": "fixture.json",
            "summary": {
                "item_count": 2,
                "kernel_tokens": 300,
                "baseline_tokens": 500,
                "savings_tokens": 200,
                "savings_percent": 40.0,
                "average_savings_percent": 42.0,
                "execution_tokens": 100,
                "passed_checks": 5,
                "total_checks": 5,
                "executed_items": 2,
                "blocked_items": 0,
            },
            "hotspots": [
                {"id": "task-a", "kernel_tokens": 160, "baseline_tokens": 220, "savings_percent": 27.27, "execution_tokens": 45, "checks": "3/3"},
            ],
            "low_savings": [
                {"id": "task-a", "kernel_tokens": 160, "baseline_tokens": 220, "savings_percent": 27.27, "execution_tokens": 45, "checks": "3/3"},
            ],
            "items": [
                {"id": "task-a", "kernel_tokens": 160, "baseline_tokens": 220, "savings_tokens": 60, "savings_percent": 27.27, "execution_tokens": 45, "checks": "3/3"},
                {"id": "task-b", "kernel_tokens": 140, "baseline_tokens": 280, "savings_tokens": 140, "savings_percent": 50.0, "execution_tokens": 55, "checks": "2/2"},
            ],
        }
        after = {
            "kind": "eval",
            "id": "after",
            "name": "After",
            "source": "fixture.json",
            "summary": {
                "item_count": 2,
                "kernel_tokens": 345,
                "baseline_tokens": 505,
                "savings_tokens": 160,
                "savings_percent": 31.68,
                "average_savings_percent": 34.0,
                "execution_tokens": 132,
                "passed_checks": 5,
                "total_checks": 5,
                "executed_items": 2,
                "blocked_items": 0,
            },
            "hotspots": [
                {"id": "task-a", "kernel_tokens": 190, "baseline_tokens": 230, "savings_percent": 17.39, "execution_tokens": 70, "checks": "3/3"},
            ],
            "low_savings": [
                {"id": "task-a", "kernel_tokens": 190, "baseline_tokens": 230, "savings_percent": 17.39, "execution_tokens": 70, "checks": "3/3"},
            ],
            "items": [
                {"id": "task-a", "kernel_tokens": 190, "baseline_tokens": 230, "savings_tokens": 40, "savings_percent": 17.39, "execution_tokens": 70, "checks": "3/3"},
                {"id": "task-b", "kernel_tokens": 155, "baseline_tokens": 275, "savings_tokens": 120, "savings_percent": 43.64, "execution_tokens": 62, "checks": "2/2"},
            ],
        }

        diff = diff_cost_reports(before, after, token_tolerance=10)

        self.assertFalse(diff["ok"])
        self.assertEqual(diff["hotspot_change"]["after_scope"], "task-a")
        self.assertLess(diff["weakest_savings_change"]["metric_delta"], 0)
        self.assertGreaterEqual(len(diff["regressions"]), 3)
        self.assertTrue(any(item["kind"] == "execution_tokens" for item in diff["regressions"]))
        self.assertTrue(any(item["kind"] == "hotspot_kernel_tokens" for item in diff["regressions"]))
        self.assertTrue(any(item["kind"] == "weakest_savings_percent" for item in diff["regressions"]))

    def test_memory_retrieval_rejects_single_weak_match_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            memory = MemoryStore(workspace)
            memory.add("fact", "Extra unrelated memory increases baseline context.", ["noise"])

            results = memory.search("Edit a documentation file while preserving unrelated changes")

            self.assertEqual(results, [])
            self.assertFalse(is_relevant_memory_match(["extra"], []))
            self.assertTrue(is_relevant_memory_match(["context"], ["context"]))

    def test_memory_store_dedupes_updates_forgets_and_migrates_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            Workspace.append_jsonl(
                workspace.memory_file,
                [
                    {
                        "id": "legacy123",
                        "kind": "decision",
                        "text": "Keep the first release CLI-only.",
                        "tags": ["legacy"],
                        "created_at": "2026-05-11T00:00:00+00:00",
                    }
                ],
            )

            memory = MemoryStore(workspace)
            self.assertEqual(memory.get("legacy123").kind, "decision")
            first = memory.add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            duplicate = memory.add("preference", " Prefer   CLI-first context budget prototypes. ", ["mvp"])

            self.assertEqual(first.id, duplicate.id)
            self.assertEqual(duplicate.tags, ["cli", "mvp"])

            updated = memory.update(first.id, text="Prefer lean CLI-first context budget prototypes.", tags=["lean"])
            self.assertEqual(updated.tags, ["lean"])
            self.assertIn("lean", updated.text)
            self.assertTrue(memory.forget(first.id))
            self.assertRaises(KeyError, memory.get, first.id)
            self.assertEqual(memory.get(first.id, include_archived=True).archived_at is not None, True)

    def test_memory_prune_keeps_pinned_records_and_archives_lower_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            memory = MemoryStore(workspace)
            pinned = memory.add("preference", "Always keep CLI-first project preferences.", ["keep"])
            stale = memory.add("task_state", "Old transient task detail that can be recovered from traces.", ["stale"])

            dry_run = memory.prune(max_records=1, dry_run=True)
            result = memory.prune(max_records=1)

            self.assertEqual(dry_run["candidate_count"], 1)
            self.assertEqual(result["archived"], 1)
            self.assertEqual(memory.get(pinned.id).id, pinned.id)
            self.assertRaises(KeyError, memory.get, stale.id)

    def test_memory_prune_explains_recoverability_and_prefers_irrecoverable_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            memory = MemoryStore(workspace)
            pinned = memory.add("preference", "Always preserve user launch preferences.", ["keep"])
            recoverable = memory.add("task_state", "Auto summary that is also stored in trace state.", ["auto"])
            project_state = memory.add("project_state", "Current local architecture decision is not in traces.", ["design"])
            Workspace.write_json(
                workspace.traces_dir / "trace123.json",
                {
                    "id": "trace123",
                    "state": {"records": [recoverable.to_dict()]},
                },
            )

            dry_run = memory.prune(max_records=2, dry_run=True)
            result = memory.prune(max_records=2)

            self.assertEqual(dry_run["recoverable_candidates"], 1)
            self.assertEqual(dry_run["candidate_decisions"][0]["record"]["id"], recoverable.id)
            self.assertIn("trace_recoverable-18", dry_run["candidate_decisions"][0]["reasons"])
            self.assertEqual(memory.get(pinned.id).id, pinned.id)
            self.assertEqual(memory.get(project_state.id).id, project_state.id)
            self.assertRaises(KeyError, memory.get, recoverable.id)
            self.assertEqual(result["archived"], 1)

    def test_memory_audit_cli_reports_scores_and_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            MemoryStore(workspace).add("preference", "Prefer compact checkpoints.", ["keep"])

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "memory", "audit"])

            output = stdout.getvalue()
            self.assertIn("score=", output)
            self.assertIn("reasons:", output)
            self.assertIn("pinned:keep+100", output)

    def test_global_memory_push_and_pull_are_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_a = Workspace(root / "project-a")
            project_b = Workspace(root / "project-b")
            project_a.init()
            project_b.init()
            MemoryStore(project_a).add("preference", "Prefer compact context packets across projects.", ["cli"])

            pushed = push_global_memories(project_a, global_root=root / "global")
            pulled = pull_global_memories(project_b, global_root=root / "global")
            pulled_again = pull_global_memories(project_b, global_root=root / "global")

            self.assertEqual(pushed["count"], 1)
            self.assertEqual(pulled["count"], 1)
            self.assertEqual(pulled_again["count"], 1)
            records = MemoryStore(project_b).all()
            self.assertEqual(len(records), 1)
            self.assertIn("global", records[0].tags)

    def test_global_memory_sync_supports_preview_namespace_and_source_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_a = Workspace(root / "alpha")
            project_b = Workspace(root / "beta")
            project_a.init()
            project_b.init()
            MemoryStore(project_a).add("preference", "Prefer compact global context.", ["team"])
            MemoryStore(project_a).add("fact", "Private alpha-only implementation note.", ["private"])

            preview_push = push_global_memories(
                project_a,
                namespace="Team Runtime",
                tag="team",
                dry_run=True,
                global_root=root / "global",
            )
            pushed = push_global_memories(
                project_a,
                namespace="Team Runtime",
                tag="team",
                global_root=root / "global",
            )
            preview_pull = pull_global_memories(
                project_b,
                namespace="team-runtime",
                source_project="alpha",
                dry_run=True,
                global_root=root / "global",
            )
            pulled = pull_global_memories(
                project_b,
                namespace="team-runtime",
                source_project="alpha",
                global_root=root / "global",
            )

            self.assertEqual(preview_push["count"], 0)
            self.assertEqual(preview_push["candidate_count"], 1)
            self.assertEqual(pushed["count"], 1)
            self.assertIn("namespace:team-runtime", pushed["records"][0]["tags"])
            self.assertIn("source_project:alpha", pushed["records"][0]["tags"])
            self.assertEqual(preview_pull["count"], 0)
            self.assertEqual(preview_pull["candidate_count"], 1)
            self.assertEqual(pulled["count"], 1)
            pulled_records = MemoryStore(project_b).all()
            self.assertEqual(len(pulled_records), 1)
            self.assertIn("imported_global", pulled_records[0].tags)
            self.assertNotIn("Private alpha-only", pulled_records[0].text)

    def test_global_memory_cli_dry_run_previews_without_copying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_a = Workspace(root / "alpha")
            project_a.init()
            MemoryStore(project_a).add("decision", "Share release checklist globally.", ["release"])

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(
                    [
                        "--workspace",
                        str(project_a.root),
                        "memory",
                        "global-push",
                        "--namespace",
                        "release",
                        "--tag",
                        "release",
                        "--dry-run",
                        "--global-root",
                        str(root / "global"),
                    ]
                )

            output = stdout.getvalue()
            self.assertIn("would copy 1 memory", output)
            self.assertEqual(MemoryStore(Workspace(root / "global")).all(), [])

    def test_packaged_skill_marketplace_installs_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            skills = list_marketplace_skills()
            installed = install_marketplace_skill(workspace, "multi_file_bugfix")

            self.assertTrue(any(skill.get("id") == "multi_file_bugfix" for skill in skills))
            self.assertTrue(all("version" in skill for skill in skills))
            self.assertTrue(all(skill.get("compatibility_check", {}).get("ok") for skill in skills))
            self.assertEqual(installed["id"], "multi_file_bugfix")
            self.assertEqual(installed["version"], "0.1.0")
            self.assertTrue(installed["compatibility"]["ok"])
            self.assertEqual(SkillRegistry(workspace).get("multi_file_bugfix").name, "Multi File Bugfix")

    def test_marketplace_blocks_incompatible_or_untrusted_remote_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root / "workspace")
            workspace.init()
            skill_path = root / "sample_skill.json"
            Workspace.write_json(
                skill_path,
                {
                    "id": "sample_skill",
                    "name": "Sample Skill",
                    "summary": "Sample marketplace skill.",
                    "intent": "Exercise marketplace compatibility checks.",
                    "inputs": ["task"],
                    "outputs": ["result"],
                    "constraints": ["test only"],
                    "failure_modes": ["none"],
                    "procedure": ["return a result"],
                    "examples": [],
                },
            )
            incompatible_index = root / "incompatible-index.json"
            Workspace.write_json(
                incompatible_index,
                {
                    "version": 2,
                    "name": "Incompatible Test Market",
                    "skills": [
                        {
                            "id": "sample_skill",
                            "name": "Sample Skill",
                            "summary": "Sample marketplace skill.",
                            "version": "9.0.0",
                            "compatibility": {"context_kernel": ">=9.0.0"},
                            "path": "sample_skill.json",
                        }
                    ],
                },
            )
            remote_index = root / "remote-index.json"
            Workspace.write_json(
                remote_index,
                {
                    "version": 2,
                    "name": "Remote Test Market",
                    "skills": [
                        {
                            "id": "remote_skill",
                            "name": "Remote Skill",
                            "summary": "Remote marketplace skill.",
                            "version": "0.1.0",
                            "compatibility": {"context_kernel": ">=0.1.0"},
                            "path": "https://example.invalid/remote_skill.json",
                        }
                    ],
                },
            )

            with self.assertRaises(ValueError):
                install_marketplace_skill(workspace, "sample_skill", index=incompatible_index)
            with self.assertRaises(PermissionError):
                install_marketplace_skill(workspace, "remote_skill", index=remote_index)

            installed = install_marketplace_skill(
                workspace,
                "sample_skill",
                index=incompatible_index,
                ignore_compat=True,
            )
            self.assertEqual(installed["id"], "sample_skill")

    def test_marketplace_cli_lists_version_remote_and_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "skill", "market-list"])

            output = stdout.getvalue()
            self.assertIn("multi_file_bugfix", output)
            self.assertIn("v0.1.0", output)
            self.assertIn("compat=ok", output)

    def test_execution_planner_reports_route_budget_and_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])

            plan = ExecutionPlanner(workspace).plan("Implement a CLI context budget prototype", 900)

            self.assertEqual(plan["route"]["mode"], "code_or_file_work")
            self.assertEqual(len(plan["selection"]["skills"]), 1)
            self.assertEqual(len(plan["selection"]["memory"]), 1)
            self.assertGreater(plan["savings"]["estimated_tokens"], 0)
            self.assertFalse(plan["budget"]["over_budget"])

    def test_workspace_load_config_backfills_command_policy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            Workspace.write_json(
                workspace.config_file,
                {
                    "version": 1,
                    "default_budget": 900,
                    "runtime_instructions": ["Legacy runtime guidance."],
                },
            )

            config = workspace.load_config()

            self.assertEqual(config["version"], 2)
            self.assertEqual(config["default_budget"], 900)
            self.assertEqual(config["runtime_instructions"], ["Legacy runtime guidance."])
            self.assertIn("python", config["command_policy"]["allowed_roots"])

    def test_workspace_command_allowlist_flows_into_policy_tools_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            config = workspace.load_config()
            config["command_policy"]["allowed_roots"].append("hostname")
            workspace.save_config(config)

            policy = check_command_policy("hostname", workspace=workspace)
            result = ToolExecutor(workspace).run_command("hostname")
            packet = ContextBuilder(workspace).build("Run command hostname and report it.", 900)

            self.assertTrue(policy["allowed"])
            self.assertTrue(result["ok"])
            self.assertIn("hostname", packet["runtime"]["command_policy"]["allowed_roots"])

    def test_policy_contracts_block_sensitive_files_and_destructive_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            safe_write = check_file_policy(workspace, "write", "src/example.py")
            secret_read = check_file_policy(workspace, "read", ".env")
            outside_read = check_file_policy(workspace, "read", Path(tmp).parent / "outside.txt")
            default_delete = check_file_policy(workspace, "delete", "src/example.py")
            allowed_delete = check_file_policy(workspace, "delete", "src/example.py", allow_destructive=True)
            safe_command = check_command_policy("python -m unittest discover -s tests")
            destructive_command = check_command_policy("git reset --hard")
            explicitly_allowed_destructive_command = check_command_policy("git reset --hard", allow_destructive=True)

            self.assertTrue(safe_write["allowed"])
            self.assertFalse(secret_read["allowed"])
            self.assertFalse(outside_read["allowed"])
            self.assertFalse(default_delete["allowed"])
            self.assertTrue(allowed_delete["allowed"])
            self.assertTrue(safe_command["allowed"])
            self.assertFalse(destructive_command["allowed"])
            self.assertTrue(explicitly_allowed_destructive_command["allowed"])

    def test_tool_executor_runs_only_policy_allowed_operations_and_traces_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            executor = ToolExecutor(workspace)

            listed_root = executor.list_dir(".")
            missing_info = executor.file_info("notes/missing.txt")
            created = executor.create_file("notes/create-only.txt", "first")
            duplicate_create = executor.create_file("notes/create-only.txt", "second")
            appended = executor.append_file("notes/create-only.txt", "\nsecond")
            append_read = executor.read_file("notes/create-only.txt")
            written = executor.write_file("notes/result.txt", "hello tool layer")
            read = executor.read_file("notes/result.txt")
            patched = executor.patch_file("notes/result.txt", "tool layer", "policy tool layer")
            after_patch = executor.read_file("notes/result.txt")
            (Path(tmp) / "notes" / "bom.txt").write_text("bom content", encoding="utf-8-sig")
            bom_read = executor.read_file("notes/bom.txt")
            duplicate_source = executor.write_file("notes/duplicate.txt", "same same")
            failed_patch = executor.patch_file("notes/duplicate.txt", "same", "other")
            replace_all_source = executor.write_file("notes/multi.txt", "same same same")
            replace_all_patch = executor.patch_file("notes/multi.txt", "same", "other", replace_all=True)
            replace_all_read = executor.read_file("notes/multi.txt")
            occurrence_source = executor.write_file("notes/occurrence.txt", "same same same")
            occurrence_patch = executor.patch_file("notes/occurrence.txt", "same", "other", occurrence=2)
            occurrence_read = executor.read_file("notes/occurrence.txt")
            anchor_source = executor.write_file("notes/block.md", "alpha\n<!-- START -->\nold body\n<!-- END -->\nomega\n")
            anchor_patch = executor.patch_file(
                "notes/block.md",
                new="new body",
                start_anchor="<!-- START -->",
                end_anchor="<!-- END -->",
            )
            anchor_read = executor.read_file("notes/block.md")
            inclusive_source = executor.write_file("notes/inclusive.md", "head\n[[BEGIN]]\nold\n[[END]]\nfoot\n")
            inclusive_patch = executor.patch_file(
                "notes/inclusive.md",
                new="[[BEGIN]]\nnew\n[[END]]",
                start_anchor="[[BEGIN]]",
                end_anchor="[[END]]",
                include_anchors=True,
            )
            inclusive_read = executor.read_file("notes/inclusive.md")
            blocked_read = executor.read_file(".env")
            command = executor.run_command("python -c \"print(123)\"")
            blocked_command = executor.run_command("git reset --hard")
            blocked_delete = executor.delete_file("notes/result.txt")
            deleted = executor.delete_file("notes/result.txt", allow_destructive=True)
            traces = executor.list_traces()
            loaded = executor.get_trace(written["id"])

            self.assertTrue(listed_root["ok"])
            self.assertIn("entries", listed_root["output"])
            self.assertTrue(missing_info["ok"])
            self.assertFalse(missing_info["output"]["exists"])
            self.assertTrue(created["ok"])
            self.assertEqual(created["tool"], "create_file")
            self.assertEqual(created["output"]["transaction"]["status"], "committed")
            self.assertEqual(created["output"]["transaction"]["snapshot_count"], 1)
            self.assertFalse(duplicate_create["ok"])
            self.assertEqual(duplicate_create["tool"], "create_file")
            self.assertIn("already exists", duplicate_create["error"])
            self.assertEqual((Path(tmp) / "notes" / "create-only.txt").read_text(encoding="utf-8"), "first\nsecond")
            self.assertTrue(appended["ok"])
            self.assertEqual(appended["tool"], "append_file")
            self.assertEqual(appended["output"]["transaction"]["status"], "committed")
            self.assertEqual(append_read["output"]["content"], "first\nsecond")
            self.assertTrue(written["ok"])
            self.assertEqual(written["output"]["transaction"]["status"], "committed")
            self.assertTrue(read["ok"])
            self.assertEqual(read["output"]["content"], "hello tool layer")
            self.assertTrue(patched["ok"])
            self.assertEqual(patched["output"]["transaction"]["status"], "committed")
            self.assertEqual(after_patch["output"]["content"], "hello policy tool layer")
            self.assertEqual(bom_read["output"]["content"], "bom content")
            self.assertTrue(duplicate_source["ok"])
            self.assertFalse(failed_patch["ok"])
            self.assertEqual(failed_patch["output"]["matches"], 2)
            self.assertTrue(replace_all_source["ok"])
            self.assertTrue(replace_all_patch["ok"])
            self.assertEqual(replace_all_patch["output"]["mode"], "replace_all")
            self.assertEqual(replace_all_patch["output"]["replacement_count"], 3)
            self.assertEqual(replace_all_read["output"]["content"], "other other other")
            self.assertTrue(occurrence_source["ok"])
            self.assertTrue(occurrence_patch["ok"])
            self.assertEqual(occurrence_patch["output"]["mode"], "occurrence:2")
            self.assertEqual(occurrence_patch["output"]["replacement_count"], 1)
            self.assertEqual(occurrence_read["output"]["content"], "same other same")
            self.assertTrue(anchor_source["ok"])
            self.assertTrue(anchor_patch["ok"])
            self.assertEqual(anchor_patch["output"]["mode"], "anchor_between")
            self.assertEqual(anchor_read["output"]["content"], "alpha\n<!-- START -->\nnew body\n<!-- END -->\nomega\n")
            self.assertTrue(inclusive_source["ok"])
            self.assertTrue(inclusive_patch["ok"])
            self.assertEqual(inclusive_patch["output"]["mode"], "anchor_inclusive")
            self.assertEqual(inclusive_read["output"]["content"], "head\n[[BEGIN]]\nnew\n[[END]]\nfoot\n")
            self.assertTrue(blocked_read["blocked"])
            self.assertTrue(command["ok"])
            self.assertIn("123", command["output"]["stdout"])
            self.assertTrue(blocked_command["blocked"])
            self.assertTrue(blocked_delete["blocked"])
            self.assertTrue(deleted["ok"])
            self.assertFalse((Path(tmp) / "notes" / "result.txt").exists())
            self.assertGreaterEqual(len(traces), 24)
            self.assertEqual(loaded["id"], written["id"])

    def test_tool_cli_exposes_stable_file_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "tool", "create", "notes/cli.txt", "--text", "first"])
            self.assertIn("ok: create_file", stdout.getvalue())
            self.assertIn("transaction:", stdout.getvalue())
            self.assertIn("files: committed", stdout.getvalue())

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "tool", "append", "notes/cli.txt", "--text", "\nsecond"])
            self.assertIn("ok: append_file", stdout.getvalue())
            self.assertIn("transaction:", stdout.getvalue())

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "tool", "file-info", "notes/cli.txt"])
            self.assertIn("kind: file", stdout.getvalue())

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "tool", "list-dir", "notes"])
            self.assertIn("cli.txt", stdout.getvalue())

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "tool", "create", "notes/cli.txt", "--text", "overwrite"])
            failed_output = stdout.getvalue()
            self.assertIn("failed: create_file", failed_output)
            self.assertIn("next: inspect the saved trace", failed_output)
            self.assertIn("inspect: akernel trace show", failed_output)

            self.assertEqual((Path(tmp) / "notes" / "cli.txt").read_text(encoding="utf-8"), "first\nsecond")
            tools = {trace["tool"] for trace in ToolExecutor(workspace).list_traces()}
            self.assertTrue({"create_file", "append_file", "file_info", "list_dir"}.issubset(tools))

    def test_tool_exec_folds_long_stdout_with_trace_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "tool",
                        "exec",
                        "--",
                        "python",
                        "-c",
                        "print(chr(120)*3000)",
                    ]
                )

            output = stdout.getvalue()
            self.assertIn("ok: run_command", output)
            self.assertIn("stdout folded", output)
            self.assertIn("akernel tool show", output)

    def test_tool_executor_batch_patch_applies_multiple_edits_and_rolls_back_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            executor = ToolExecutor(workspace)
            executor.write_file("notes/a.txt", "hello old")
            executor.write_file("notes/b.txt", "alpha\n<!-- START -->\nold body\n<!-- END -->\nomega\n")

            success = executor.batch_patch(
                [
                    {"path": "notes/a.txt", "old": "old", "new": "new"},
                    {
                        "path": "notes/b.txt",
                        "start_anchor": "<!-- START -->",
                        "end_anchor": "<!-- END -->",
                        "new": "fresh body",
                    },
                ]
            )
            failed = executor.batch_patch(
                [
                    {"path": "notes/a.txt", "old": "new", "new": "changed"},
                    {"path": "notes/b.txt", "old": "missing", "new": "changed"},
                ]
            )

            self.assertTrue(success["ok"])
            self.assertEqual(success["output"]["applied_count"], 2)
            self.assertEqual(success["output"]["transaction"]["status"], "committed")
            self.assertFalse(success["output"]["transaction"]["rolled_back"])
            self.assertEqual(success["output"]["transaction"]["snapshot_count"], 2)
            self.assertEqual((Path(tmp) / "notes" / "a.txt").read_text(encoding="utf-8"), "hello new")
            self.assertEqual(
                (Path(tmp) / "notes" / "b.txt").read_text(encoding="utf-8"),
                "alpha\n<!-- START -->\nfresh body\n<!-- END -->\nomega\n",
            )
            self.assertFalse(failed["ok"])
            self.assertTrue(failed["output"]["rolled_back"])
            self.assertEqual(failed["output"]["transaction"]["status"], "rolled_back")
            self.assertTrue(failed["output"]["transaction"]["rollback"]["restored"])
            self.assertEqual((Path(tmp) / "notes" / "a.txt").read_text(encoding="utf-8"), "hello new")

            spec = Path(tmp) / "patch-spec.json"
            spec.write_text(
                json.dumps([{"path": "notes/a.txt", "old": "new", "new": "newer"}]),
                encoding="utf-8",
            )
            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "tool", "batch-patch", "--specs-file", str(spec)])
            output = stdout.getvalue()
            self.assertIn("transaction:", output)
            self.assertIn("committed", output)

    def test_tool_transaction_rolls_back_file_steps_when_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            executor = ToolExecutor(workspace)
            executor.write_file("notes/a.txt", "hello")

            success = executor.transaction(
                [
                    {"action": "append_file", "path": "notes/a.txt", "text": " world"},
                    {"action": "patch_file", "path": "notes/a.txt", "old": "world", "new": "agent"},
                    {"action": "run_command", "command": "python -c \"print('ok')\""},
                ]
            )
            failed = executor.transaction(
                [
                    {"action": "append_file", "path": "notes/a.txt", "text": " again"},
                    {"action": "create_file", "path": "notes/new.txt", "text": "temporary"},
                    {"action": "run_command", "command": "python -c \"import sys; sys.exit(2)\""},
                ]
            )
            blocked_command = executor.transaction(
                [
                    {"action": "append_file", "path": "notes/a.txt", "text": " blocked"},
                    {"action": "run_command", "command": "git reset --hard"},
                ]
            )
            blocked_path = executor.transaction(
                [
                    {"action": "write_file", "path": "../outside.txt", "text": "outside"},
                ]
            )

            self.assertTrue(success["ok"])
            self.assertFalse(success["output"]["rolled_back"])
            self.assertEqual(success["output"]["transaction"]["status"], "committed")
            self.assertEqual(success["output"]["safety"]["step_count"], 3)
            self.assertEqual(success["output"]["safety"]["file_step_count"], 2)
            self.assertEqual(success["output"]["safety"]["command_step_count"], 1)
            self.assertEqual((Path(tmp) / "notes" / "a.txt").read_text(encoding="utf-8"), "hello agent")
            self.assertFalse(failed["ok"])
            self.assertIn("run_command exit_code=2", failed["error"])
            self.assertTrue(failed["output"]["rolled_back"])
            self.assertEqual(failed["output"]["transaction"]["status"], "rolled_back")
            self.assertEqual(failed["output"]["failure"]["step"], 3)
            self.assertEqual(failed["output"]["failure"]["action"], "run_command")
            self.assertIn("run_command exit_code=2", failed["output"]["results"][-1]["failure_reason"])
            self.assertEqual((Path(tmp) / "notes" / "a.txt").read_text(encoding="utf-8"), "hello agent")
            self.assertFalse((Path(tmp) / "notes" / "new.txt").exists())
            self.assertTrue(blocked_command["blocked"])
            self.assertEqual(blocked_command["output"]["failure"]["kind"], "command_policy")
            self.assertIn("step 2", blocked_command["policy"]["reasons"][0])
            self.assertEqual(blocked_command["output"]["safety"]["command_step_count"], 1)
            self.assertEqual((Path(tmp) / "notes" / "a.txt").read_text(encoding="utf-8"), "hello agent")
            self.assertTrue(blocked_path["blocked"])
            self.assertEqual(blocked_path["output"]["failure"]["kind"], "file_policy")
            self.assertFalse((Path(tmp).parent / "outside.txt").exists())

            spec = Path(tmp) / "transaction-spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "steps": [
                            {"action": "append_file", "path": "notes/a.txt", "text": " cli"},
                            {"action": "run_command", "command": "python -c \"print('ok')\""},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "tool", "transaction", "--specs-file", str(spec)])
            output = stdout.getvalue()
            self.assertIn("ok: transaction", output)
            self.assertIn("files: committed", output)
            self.assertIn("safety: steps=2 files=1 commands=1", output)
            self.assertIn("applied_count: 2", output)

    def test_agent_transaction_action_executes_with_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            executor = ToolExecutor(workspace)
            executor.write_file("notes/a.txt", "stable")

            action = parse_agent_action(
                json.dumps(
                    {
                        "action": "transaction",
                        "steps": [
                            {"action": "append_file", "path": "notes/a.txt", "text": " changed"},
                            {"action": "create_file", "path": "notes/temp.txt", "text": "temporary"},
                            {"action": "run_command", "command": "python -c \"import sys; sys.exit(2)\""},
                        ],
                    }
                )
            )
            result = execute_agent_action(executor, action)

            self.assertEqual(action["action"], "transaction")
            self.assertFalse(result["ok"])
            self.assertEqual(result["tool"], "transaction")
            self.assertIn("run_command exit_code=2", result["error"])
            self.assertTrue(result["output"]["rolled_back"])
            self.assertEqual(result["output"]["transaction"]["status"], "rolled_back")
            self.assertEqual((Path(tmp) / "notes" / "a.txt").read_text(encoding="utf-8"), "stable")
            self.assertFalse((Path(tmp) / "notes" / "temp.txt").exists())

            with self.assertRaisesRegex(ValueError, "unsupported action: mcp_call"):
                parse_agent_action(
                    json.dumps(
                        {
                            "action": "transaction",
                            "steps": [{"action": "mcp_call", "server": "fake", "tool": "echo"}],
                        }
                    )
                )

    def test_batch_patch_spec_loader_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "patch-spec.json"
            spec.write_text('\ufeff{"edits":[{"path":"notes/a.txt","old":"old","new":"new"}]}', encoding="utf-8")

            edits = load_batch_patch_specs(spec)

            self.assertEqual(edits, [{"path": "notes/a.txt", "old": "old", "new": "new"}])

    def test_transaction_spec_loader_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "transaction-spec.json"
            spec.write_text('\ufeff{"steps":[{"action":"run_command","command":"python -V"}]}', encoding="utf-8")

            steps = load_transaction_specs(spec)

            self.assertEqual(steps, [{"action": "run_command", "command": "python -V"}])

    def test_task_store_tracks_checkpoints_refs_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            store = TaskStore(workspace)

            task = store.start("Build CLI task sessions", goal="Make task progress resumable.")
            task = store.step(task["id"], "Implemented task store.")
            memory = MemoryStore(workspace).add("task_state", "Task session layer is implemented.", ["task"])
            run_trace = AgentRunner(workspace).run("Summarize the task layer", provider_name="mock", budget=900)
            tool_trace = ToolExecutor(workspace).write_file("notes/task.txt", "checkpoint")
            task = store.attach(task["id"], "tool", "tool123")
            task = store.attach(task["id"], "run", "run123")
            task = store.attach(task["id"], "memory", memory.id)
            task = store.attach(task["id"], "run", run_trace["id"])
            task = store.attach(task["id"], "tool", tool_trace["id"])
            summary = store.summary(task["id"])
            brief = store.brief(task["id"])
            blocked = store.set_status(task["id"], "blocked", "Need user confirmation.")
            completed = store.set_status(task["id"], "completed", "Finished task session MVP.")
            listed = store.list()
            active = store.list(status="active")

            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["refs"]["tool_traces"], ["tool123", tool_trace["id"]])
            self.assertEqual(completed["refs"]["run_traces"], ["run123", run_trace["id"]])
            self.assertEqual(summary["refs"]["tool_traces"], 2)
            self.assertEqual(summary["refs"]["run_traces"], 2)
            self.assertEqual(summary["latest_steps"][-1]["kind"], "attach")
            self.assertEqual(brief["linked_memory"][-1]["id"], memory.id)
            self.assertEqual(brief["linked_run_traces"][-1]["id"], run_trace["id"])
            self.assertEqual(brief["linked_tool_traces"][-1]["id"], tool_trace["id"])
            self.assertGreater(brief["estimated_tokens"], 0)
            self.assertEqual(listed[0]["id"], task["id"])
            self.assertEqual(active, [])
            with self.assertRaises(ValueError):
                store.step(task["id"], "Should not append after completion.")

    def test_task_store_structured_plan_checkpoint_and_resume_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            store = TaskStore(workspace)

            task = store.start(
                "Build durable long task planning",
                goal="Make long-running agent work resumable through milestones and checkpoints.",
                with_plan=True,
            )
            first_next = store.next_checkpoint(task["id"])
            checkpointed = store.checkpoint(
                task["id"],
                "Finished investigation and identified task-store extension points.",
                milestone_id="M1",
                status="completed",
            )
            brief = store.brief(task["id"])
            plan = ExecutionPlanner(workspace).plan(
                "Continue the long task implementation",
                1200,
                task_id=task["id"],
                resume=True,
            )

            self.assertEqual(first_next["milestone"]["id"], "M1")
            self.assertIn("Acceptance:", first_next["resume_prompt"])
            self.assertEqual(checkpointed["plan"]["milestones"][0]["status"], "completed")
            self.assertEqual(checkpointed["plan"]["milestones"][1]["status"], "active")
            self.assertEqual(brief["plan"]["active_milestone"]["id"], "M2")
            self.assertEqual(brief["plan"]["progress"]["completed"], 1)
            self.assertEqual(plan["task"]["plan"]["active_milestone"]["id"], "M2")

    def test_task_plan_cli_commands_manage_long_task_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "task",
                        "start",
                        "Ship agent planner",
                        "--goal",
                        "Add resumable task planning.",
                        "--plan",
                    ]
                )
            start_output = stdout.getvalue()
            task_id = start_output.split()[1]

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "task", "next", task_id])
            self.assertIn("next: M1", stdout.getvalue())

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "task",
                        "checkpoint",
                        task_id,
                        "--note",
                        "Investigation done.",
                        "--milestone",
                        "M1",
                        "--status",
                        "completed",
                    ]
                )
            self.assertIn("1/5 completed", stdout.getvalue())

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "task", "brief", task_id])
            brief_output = stdout.getvalue()
            self.assertIn("active: M2", brief_output)
            self.assertIn(f"continue: akernel agent run --task {task_id}", brief_output)
            self.assertIn(f"next: akernel task next {task_id}", brief_output)

    def test_task_resume_context_flows_into_context_plan_and_run_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            store = TaskStore(workspace)
            task = store.start("Resume context task", goal="Carry compact task state into model calls.")
            memory = MemoryStore(workspace).add("task_state", "Resume context is ready.", ["resume"])
            store.attach(task["id"], "memory", memory.id)

            packet = ContextBuilder(workspace).build(
                "Continue from the checkpoint",
                900,
                task_id=task["id"],
                resume=True,
            )
            plan = ExecutionPlanner(workspace).plan(
                "Continue from the checkpoint",
                900,
                task_id=task["id"],
                resume=True,
            )
            trace = AgentRunner(workspace).run(
                "Continue from the checkpoint",
                provider_name="mock",
                budget=900,
                task_id=task["id"],
                resume=True,
            )

            self.assertTrue(packet["task"]["resume"])
            self.assertEqual(packet["task"]["brief"]["task"]["id"], task["id"])
            self.assertEqual(packet["task"]["brief"]["linked_memory"][0]["id"], memory.id)
            self.assertTrue(plan["task"]["resume"])
            self.assertEqual(plan["task"]["id"], task["id"])
            self.assertTrue(trace["resume"])
            self.assertEqual(trace["task_id"], task["id"])
            self.assertTrue(trace["context_packet"]["task"]["resume"])

    def test_agent_loop_creates_resumable_task_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            report = AgentLoop(workspace).run(
                "Continue the agent runtime implementation",
                provider_name="mock",
                budget=900,
                max_steps=1,
                remember=True,
            )
            task = TaskStore(workspace).get(report["task_id"])
            saved = Workspace.read_json(workspace.agent_runs_dir / f"{report['id']}.json")

            self.assertEqual(report["status"], "responded")
            self.assertEqual(len(report["steps"]), 1)
            self.assertTrue(report["steps"][0]["verifier_ok"])
            self.assertEqual(report["model_routing"]["mode"], "primary")
            self.assertEqual(report["steps"][0]["model_role"], "primary")
            self.assertGreater(report["totals"]["total_tokens"], 0)
            self.assertIn("Mock agent response", report["final_response"])
            self.assertTrue((workspace.agent_runs_dir / f"{report['id']}.json").exists())
            self.assertEqual(saved["storage"]["detail_level"], "compact_v1")
            self.assertEqual(saved["storage"]["step_count"], 1)
            self.assertIn("model_routing", saved)
            self.assertEqual(saved["steps"][0]["model_role"], "primary")
            self.assertEqual(saved["steps"][0]["trace_id"], report["steps"][0]["trace_id"])
            self.assertNotIn("allocated", saved["steps"][0]["plan"]["budget"])
            self.assertEqual(len(task["refs"]["run_traces"]), 1)
            self.assertEqual(len(task["refs"]["memories"]), 1)
            self.assertEqual(report["state"]["written_count"], 1)
            self.assertTrue(any(step["kind"] == "agent_response" for step in task["steps"]))
            self.assertTrue(any(step["kind"] == "agent_stop" for step in task["steps"]))

    def test_agent_cost_report_reads_compact_saved_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "plan.txt").write_text("ship tool planning next", encoding="utf-8")

            report = AgentLoop(workspace).run(
                "Read notes/plan.txt and tell me what it says.",
                provider_name="mock",
                budget=1200,
                max_steps=2,
                remember=True,
            )
            saved = Workspace.read_json(workspace.agent_runs_dir / f"{report['id']}.json")

            cost = build_agent_cost_report(saved)
            rendered = render_agent_cost_report(cost)

            self.assertEqual(cost["run_id"], report["id"])
            self.assertEqual(cost["summary"]["step_count"], 2)
            self.assertEqual(cost["summary"]["total_tokens"], report["totals"]["total_tokens"])
            self.assertIn("read_file", cost["summary"]["action_breakdown"])
            self.assertIn("respond", cost["summary"]["action_breakdown"])
            self.assertGreaterEqual(cost["summary"]["task_brief"]["peak_tokens"], 0)
            self.assertEqual(cost["hotspots"][0]["total_tokens"], max(step["total_tokens"] for step in cost["steps"]))
            self.assertIn("Step Breakdown", rendered)
            self.assertIn("actions:", rendered)

    def test_agent_loop_auto_routing_can_delegate_first_step_to_auxiliary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            report = AgentLoop(workspace).run(
                "Continue the agent runtime implementation",
                provider_name="mock",
                budget=900,
                max_steps=1,
                model_routing="auto",
                remember=False,
            )

            self.assertEqual(report["model_routing"]["mode"], "auto")
            self.assertEqual(report["steps"][0]["model_role"], "auxiliary")

    def test_agent_loop_auto_expands_default_budget_for_large_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            config = workspace.load_config()
            config["runtime_instructions"] = ["budget pressure " * 2500]
            workspace.save_config(config)
            events: list[dict[str, object]] = []

            report = AgentLoop(workspace).run(
                "Continue",
                provider_name="mock",
                budget=None,
                max_steps=1,
                remember=False,
                progress_callback=events.append,
            )

            self.assertEqual(report["status"], "responded")
            self.assertTrue(any(event.get("event") == "budget_expand" for event in events))
            self.assertGreater(report["steps"][0]["plan"]["budget"]["total"], 1200)

    def test_agent_loop_emits_context_and_tool_action_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            events: list[dict[str, object]] = []

            action = {
                "action": "write_file",
                "path": "notes/progress.txt",
                "text": "visible progress",
                "reason": "exercise action progress",
            }

            class ProgressProvider:
                name = "progress-provider"
                model = "test"

                def run(self, packet):
                    return ProviderResponse(json.dumps(action), input_tokens=10, output_tokens=8)

            with patch("context_kernel.runner.get_provider", return_value=ProgressProvider()):
                report = AgentLoop(workspace).run(
                    "Create notes/progress.txt with visible progress.",
                    provider_name="openai",
                    budget=1800,
                    max_steps=1,
                    remember=False,
                    progress_callback=events.append,
                )

            self.assertEqual(report["status"], "stopped")
            self.assertTrue((Path(tmp) / "notes" / "progress.txt").exists())
            self.assertTrue(any(event.get("event") == "context_ready" for event in events))
            start = next(event for event in events if event.get("event") == "action_start")
            end = next(event for event in events if event.get("event") == "action_end")
            self.assertEqual(start["action"], "write_file")
            self.assertEqual(start["target"], "notes/progress.txt")
            self.assertEqual(end["action"], "write_file")
            self.assertTrue(end["ok"])

    def test_agent_loop_respects_explicit_budget_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            config = workspace.load_config()
            config["runtime_instructions"] = ["hard budget " * 2500]
            workspace.save_config(config)

            report = AgentLoop(workspace).run(
                "Continue",
                provider_name="mock",
                budget=1200,
                max_steps=1,
                remember=False,
            )

            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["diagnostic"]["category"], "context_budget")

    def test_agent_loop_aux_review_runs_before_primary_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            report = AgentLoop(workspace).run(
                "Review this primary-routed request.",
                provider_name="mock",
                budget=1200,
                max_steps=1,
                model_routing="primary",
                aux_model="reviewer-small",
                remember=False,
            )
            saved = Workspace.read_json(workspace.agent_runs_dir / f"{report['id']}.json")
            cost = build_agent_cost_report(saved)
            rendered = render_agent_cost_report(cost)
            review = report["steps"][0]["aux_review"]

            self.assertTrue(review["enabled"])
            self.assertEqual(review["model"], "reviewer-small")
            self.assertEqual(review["recommendation"], "continue")
            self.assertEqual(len(list(workspace.traces_dir.glob("*.json"))), 2)
            self.assertEqual(report["totals"]["total_tokens"], report["steps"][0]["tokens"]["total_tokens"] + review["tokens"]["total_tokens"])
            self.assertTrue(saved["steps"][0]["aux_review"]["enabled"])
            self.assertIn(review["trace_id"], saved["storage"]["full_details_in"]["run_traces"])
            self.assertIn("review=", rendered)

    def test_chat_runs_agent_loop_and_cost_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            with patch("builtins.input", side_effect=["/help", "/status", "/model", "/config", "Continue the runtime work", "/cost", "/exit"]):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "chat",
                            "--provider",
                            "mock",
                            "--aux-model",
                            "gpt-5.3-codex",
                            "--max-steps",
                            "1",
                        ]
                    )

            output = stdout.getvalue()
            reports = list(workspace.agent_runs_dir.glob("*.json"))
            tasks = TaskStore(workspace).list(status="active")

            self.assertEqual(len(reports), 1)
            self.assertEqual(len(tasks), 1)
            self.assertIn("akernel", output)
            self.assertIn("focused agent workspace", output)
            self.assertIn(str(workspace.root), output)
            self.assertIn("/help commands", output)
            self.assertIn("Command Palette - Context", output)
            self.assertIn("Status Runway", output)
            self.assertIn("Runtime Deck", output)
            self.assertIn("Model Roles", output)
            self.assertIn("auxiliary", output)
            self.assertIn("model_routing", output)
            self.assertIn("AKERNEL_OPENAI_AUX_MODEL", output)
            self.assertIn("agent_run:", output)
            self.assertIn("/cost for details", output)
            self.assertIn("Mock agent response", output)
            self.assertIn("contacting primary model", output)
            self.assertIn("route=primary", output)
            self.assertIn("Step Breakdown", output)
            self.assertIn("bye", output)

    def test_chat_spinner_renders_until_background_run_finishes(self) -> None:
        args = type(
            "Args",
            (),
            {
                "model": "gpt-5.5",
                "model_routing": "primary",
            },
        )()

        def run_func(progress: object) -> dict[str, object]:
            progress({"event": "provider_start", "step": 1, "max_steps": 5, "model_role": "primary", "model": "gpt-5.5"})
            time.sleep(0.28)
            progress({"event": "step_end", "step": 1, "max_steps": 5, "status": "responded", "action": "respond", "tokens": 42})
            return {"ok": True}

        with patch("sys.stdout", new=io.StringIO()) as stdout:
            result = run_agent_with_spinner(run_func, args)

        output = stdout.getvalue()

        self.assertEqual(result, {"ok": True})
        self.assertIn("waiting for primary model gpt-5.5", output)
        self.assertIn("|", output)

    def test_spinner_messages_cover_context_actions_and_file_materialization(self) -> None:
        args = type(
            "Args",
            (),
            {
                "model": "gpt-5.5",
                "model_routing": "primary",
            },
        )()

        self.assertIn("context ready", spinner_message_from_event({"event": "context_ready", "step": 1, "max_steps": 5, "estimated_used": 120, "budget_total": 1800, "memory_count": 2, "skills": [{"id": "edit_file", "level": "l2"}]}, args))
        self.assertIn("creating or updating file", spinner_message_from_event({"event": "action_start", "step": 1, "max_steps": 5, "action": "write_file", "target": "notes/demo.txt"}, args))
        transaction_start = spinner_message_from_event({"event": "action_start", "step": 2, "max_steps": 5, "action": "transaction", "target": "append notes/plan.txt, verify python -V"}, args)
        self.assertIn("transaction: append notes/plan.txt", transaction_start)
        transaction_end = spinner_message_from_event({"event": "action_end", "step": 2, "max_steps": 5, "action": "transaction", "ok": False, "blocked": False, "summary": "run_command exit_code=2; applied_count=2; rolled_back=True; steps=3"}, args)
        self.assertIn("transaction rolled back", transaction_end)
        self.assertIn("rolled_back=True", transaction_end)
        self.assertIn("saved 1 file", spinner_message_from_event({"event": "materialize_end", "step": 1, "max_steps": 5, "paths": ["notes/demo.txt"]}, args))
        self.assertIn("recovery ready", spinner_message_from_event({"event": "recovery_end", "step": 1, "max_steps": 5, "count": 1}, args))

    def test_chat_supports_file_command_paste_and_compact_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "context.txt").write_text("attached context works", encoding="utf-8")

            with patch(
                "builtins.input",
                side_effect=[
                    "@notes/context.txt",
                    "!python -c \"print(456)\"",
                    "/compact",
                    "/paste",
                    "Use the attached context and command output.",
                    "/end",
                    "/exit",
                ],
            ):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    main(["--workspace", str(workspace.root), "chat", "--provider", "mock", "--max-steps", "1"])

            output = stdout.getvalue()
            reports = list(workspace.agent_runs_dir.glob("*.json"))
            tool_traces = list(workspace.tool_traces_dir.glob("*.json"))

            self.assertEqual(len(reports), 1)
            self.assertGreaterEqual(len(tool_traces), 2)
            self.assertIn("Attached File", output)
            self.assertIn("Command Complete", output)
            self.assertIn("Compact Brief", output)
            self.assertIn("Paste mode", output)
            self.assertIn("Mock agent response", output)

    def test_chat_extensions_panel_surfaces_mcp_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            add_mcp_server(
                workspace,
                "filesystem",
                command="python -m mcp_server_filesystem .",
                tools=["read_file:Read workspace files"],
            )

            with patch("builtins.input", side_effect=["/extensions", "/mcp", "/skills", "/exit"]):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    main(["--workspace", str(workspace.root), "chat", "--provider", "mock"])

            output = stdout.getvalue()

            self.assertIn("Extensions", output)
            self.assertIn("filesystem", output)
            self.assertIn("read_file", output)
            self.assertIn("edit_file", output)

    def test_chat_mcp_commands_refresh_call_and_toggle_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server_path = root / "fake_chat_mcp_server.py"
            server_path.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "for line in sys.stdin:",
                        "    msg = json.loads(line)",
                        "    method = msg.get('method')",
                        "    if method == 'initialize':",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2024-11-05','serverInfo':{'name':'chat-mcp'}}}), flush=True)",
                        "    elif method == 'tools/list':",
                        "        tools = [{'name':'echo','description':'Echo text'}]",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'tools':tools}}), flush=True)",
                        "    elif method == 'tools/call':",
                        "        text = msg.get('params', {}).get('arguments', {}).get('text', '')",
                        "        result = {'content':[{'type':'text','text':'chat-echo:' + text}]}",
                        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)",
                    ]
                ),
                encoding="utf-8",
            )
            workspace = Workspace(root)
            workspace.init()
            add_mcp_server(workspace, "fake", command=f'"{sys.executable}" "{server_path}"')

            with patch(
                "builtins.input",
                side_effect=[
                    "/mcp refresh fake",
                    '/mcp call fake echo --args "{\\"text\\":\\"hello\\"}"',
                    "/mcp disable fake",
                    "/mcp enable fake",
                    "/exit",
                ],
            ):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    main(["--workspace", str(workspace.root), "chat", "--provider", "mock"])

            output = stdout.getvalue()
            traces = ToolExecutor(workspace).list_traces()

            self.assertIn("refreshed MCP server: fake", output)
            self.assertIn("mcp call: fake.echo", output)
            self.assertIn("chat-echo:hello", output)
            self.assertIn("disabled MCP server: fake", output)
            self.assertIn("enabled MCP server: fake", output)
            self.assertEqual(ToolExecutor(workspace).get_trace(traces[0]["id"])["tool"], "mcp_call")

    def test_chat_skill_commands_show_recommend_and_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")

            with patch(
                "builtins.input",
                side_effect=[
                    "/skills list",
                    "/skills show edit_file --level l0",
                    "/skills recommend edit a file safely",
                    "/skills install multi_file_bugfix",
                    "/exit",
                ],
            ):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    main(["--workspace", str(workspace.root), "chat", "--provider", "mock"])

            output = stdout.getvalue()
            skills = [skill.id for skill in SkillRegistry(workspace).all()]

            self.assertIn("edit_file", output)
            self.assertIn("recommended skills", output)
            self.assertIn("installed marketplace skill: multi_file_bugfix", output)
            self.assertIn("multi_file_bugfix", skills)

    def test_tui_screen_renders_session_and_last_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            args = type(
                "Args",
                (),
                {
                    "provider": "mock",
                    "model": None,
                    "aux_model": "gpt-5.3-codex",
                    "profile": "balanced",
                    "max_steps": 2,
                },
            )()
            report = {
                "id": "run123",
                "status": "responded",
                "max_steps": 2,
                "steps": [
                    {
                        "index": 1,
                        "action": {"action": "respond"},
                        "plan": {"budget": {"total": 2400}},
                        "tokens": {"total_tokens": 42},
                    }
                ],
                "totals": {"total_tokens": 42},
                "final_response": "ready",
            }

            screen = build_chat_tui_screen(
                workspace,
                "task123",
                args,
                [{"role": "user", "title": "You", "text": "Summarize this project"}],
                report,
                ["attached context"],
                status="ready",
            )

            self.assertIn("AKERNEL // READY", screen)
            self.assertIn("provider  mock", screen)
            self.assertIn("Focus", screen)
            self.assertIn("Flow", screen)
            self.assertIn("Last Run", screen)
            self.assertIn("Steps", screen)
            self.assertIn("actions   respond", screen)
            self.assertIn("meter", screen)
            self.assertIn("42/2400", screen)
            self.assertIn("/extensions", screen)

    def test_chat_report_meter_uses_report_budget(self) -> None:
        report = {
            "id": "run123",
            "status": "responded",
            "max_steps": 2,
            "steps": [
                {
                    "index": 1,
                    "status": "responded",
                    "trace_id": "trace123",
                    "model_role": "primary",
                    "action": {"action": "respond"},
                    "plan": {"budget": {"total": 2400}},
                    "tokens": {"total_tokens": 600},
                }
            ],
            "totals": {"total_tokens": 600},
            "model_routing": {"mode": "primary"},
            "final_response": "ready",
            "state": {"enabled": False},
        }

        with patch("sys.stdout", new=io.StringIO()) as stdout:
            print_chat_report(report)

        output = stdout.getvalue()

        self.assertIn("/cost for details", output)
        self.assertIn("600", output)
        self.assertIn("agent_run:", output)

    def test_tui_chat_runs_agent_loop_after_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            with patch("builtins.input", side_effect=["Continue the runtime work", "/exit"]):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "chat",
                            "--provider",
                            "mock",
                            "--max-steps",
                            "1",
                            "--ui",
                            "tui",
                        ]
                    )

            output = stdout.getvalue()
            reports = list(workspace.agent_runs_dir.glob("*.json"))

            self.assertEqual(len(reports), 1)
            self.assertIn("AKERNEL", output)
            self.assertIn("/help commands", output)
            self.assertIn("AKERNEL", output)
            self.assertIn("Mock agent response", output)
            self.assertNotIn("AKERNEL: Assistant", output)
            self.assertIn("bye", output)
            self.assertEqual(output.count("/help commands"), 1)

    def test_tui_screen_can_render_older_history_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            args = type(
                "Args",
                (),
                {
                    "provider": "mock",
                    "model": None,
                    "aux_model": "gpt-5.3-codex",
                    "profile": "balanced",
                    "max_steps": 3,
                    "model_routing": "auto",
                    "aux_review": "auto",
                },
            )()
            transcript = [
                {"role": "user", "title": "You", "text": f"message {index}"}
                for index in range(20)
            ]

            screen = build_chat_tui_screen(
                workspace,
                "task123",
                args,
                transcript,
                None,
                [],
                status="ready",
                state={"scroll_offset": 10},
            )

            self.assertIn("History view", screen)
            self.assertIn("/down or /latest", screen)

    def test_chat_file_search_lists_and_attaches_numbered_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha.txt").write_text("alpha context works", encoding="utf-8")
            (root / "beta.txt").write_text("beta context", encoding="utf-8")
            workspace = Workspace(root)
            workspace.init()

            with patch("builtins.input", side_effect=["@", "@1", "Use the attached file", "/exit"]):
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    main(["--workspace", str(workspace.root), "chat", "--provider", "mock", "--max-steps", "1"])

            output = stdout.getvalue()
            reports = list(workspace.agent_runs_dir.glob("*.json"))
            tool_traces = list(workspace.tool_traces_dir.glob("*.json"))

            self.assertEqual(len(reports), 1)
            self.assertGreaterEqual(len(tool_traces), 1)
            self.assertIn("File Search", output)
            self.assertIn("@1", output)
            self.assertIn("Attached File", output)
            self.assertIn("Mock agent response", output)

    def test_chat_completion_items_include_commands_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("demo", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "runtime.py").write_text("print('ok')", encoding="utf-8")

            command_items = chat_completion_items(root, "/mo")
            extension_items = chat_completion_items(root, "/ext")
            file_items = chat_completion_items(root, "@run")
            inline_file_items = chat_completion_items(root, "please inspect @REA")

            self.assertIn(("/model", "show primary and auxiliary model roles"), command_items)
            self.assertIn(("/extensions", "show MCP servers and registered skills"), extension_items)
            self.assertIn(("@src/runtime.py", "attach file"), file_items)
            self.assertIn(("@README.md", "attach file"), inline_file_items)

    def test_tui_report_defaults_to_clean_assistant_response(self) -> None:
        report = {
            "id": "run123",
            "status": "responded",
            "max_steps": 5,
            "totals": {"total_tokens": 123},
            "steps": [{"action": {"action": "respond"}}],
            "final_response": "你好！很高兴见到你。",
        }

        text = format_tui_report(report)

        self.assertEqual(text, "你好！很高兴见到你。")
        self.assertNotIn("status:", text)
        self.assertNotIn("agent_run:", text)

    def test_tui_report_includes_command_failure_reason(self) -> None:
        report = {
            "id": "run123",
            "task_id": "task123",
            "status": "needs_review",
            "max_steps": 5,
            "totals": {"total_tokens": 123},
            "steps": [{"action": {"action": "run_command"}}],
            "final_response": None,
            "diagnostic": {
                "category": "command_failed",
                "message": "command=python missing.py; exit_code=2; stderr=No such file",
                "suggestion": "Fix the command and rerun the task.",
            },
        }

        text = format_tui_report(report)

        self.assertIn("Run failed: command_failed", text)
        self.assertIn("Outcome: needs_review", text)
        self.assertIn("Reason: command=python missing.py; exit_code=2; stderr=No such file", text)
        self.assertIn("Next: Fix the command and rerun the task.", text)
        self.assertIn("Resume: akernel task brief task123", text)

    def test_chat_inline_file_reference_attaches_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("inline context works", encoding="utf-8")
            workspace = Workspace(root)
            workspace.init()

            with patch("builtins.input", side_effect=["Summarize @notes.md", "/exit"]):
                with patch("sys.stdout", new=io.StringIO()):
                    main(["--workspace", str(workspace.root), "chat", "--provider", "mock", "--max-steps", "1"])

            reports = list(workspace.agent_runs_dir.glob("*.json"))
            tool_traces = list(workspace.tool_traces_dir.glob("*.json"))
            saved = Workspace.read_json(reports[0])

            self.assertEqual(len(reports), 1)
            self.assertGreaterEqual(len(tool_traces), 1)
            self.assertIn("inline context works", saved["request"])

    def test_custom_slash_command_expands_saved_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command_dir = root / ".akernel" / "commands"
            command_dir.mkdir(parents=True)
            (command_dir / "review.md").write_text(
                "---\ndescription: Review the current change\n---\nReview this change: {{args}}",
                encoding="utf-8",
            )
            workspace = Workspace(root)
            workspace.init()

            items = chat_completion_items(root, "/rev")
            expanded = expand_custom_chat_command(root, "/review cli polish")

            self.assertIn(("/review", "project command: Review the current change"), items)
            self.assertEqual(expanded, "Review this change: cli polish")

    def test_tui_screen_surfaces_task_plan_and_command_strip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            task = TaskStore(workspace).start(
                "Ship polished TUI",
                goal="Make the interactive agent workspace easier to scan.",
                with_plan=True,
            )
            args = type(
                "Args",
                (),
                {
                    "provider": "mock",
                    "model": None,
                    "aux_model": "gpt-5.3-codex",
                    "profile": "balanced",
                    "max_steps": 3,
                    "model_routing": "auto",
                    "aux_review": "auto",
                },
            )()

            screen = build_chat_tui_screen(
                workspace,
                task["id"],
                args,
                [{"role": "system", "title": "Welcome", "text": "Ready for focused tasks."}],
                None,
                [],
                status="ready",
            )

            self.assertIn("AKERNEL // READY", screen)
            self.assertIn("/compact", screen)
            self.assertIn("small context, visible actions", screen)
            self.assertIn("Next", screen)
            self.assertIn("plan, act, trace", screen)
            self.assertIn("Task", screen)
            self.assertIn("plan", screen)
            self.assertIn("active", screen)

    def test_bare_akernel_starts_chat_and_initializes_default_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with patch("builtins.input", side_effect=["/exit"]):
                    with patch("sys.stdout", new=io.StringIO()) as stdout:
                        main([])
            finally:
                os.chdir(previous)

            output = stdout.getvalue()
            self.assertTrue((Path(tmp) / ".akernel").exists())
            self.assertIn("initialized workspace:", output)
            self.assertIn("focused agent workspace", output)
            self.assertIn("bye", output)

    def test_bare_akernel_accepts_chat_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with patch("builtins.input", side_effect=["/exit"]):
                    with patch("sys.stdout", new=io.StringIO()) as stdout:
                        main(["--provider", "mock", "--max-steps", "1"])
            finally:
                os.chdir(previous)

            output = stdout.getvalue()
            self.assertTrue((Path(tmp) / ".akernel").exists())
            self.assertIn("focused agent workspace", output)
            self.assertIn("task ", output)
            self.assertIn("mock | gpt-5.5 | balanced | max 1", output)

    def test_agent_action_parser_accepts_common_tool_call_shapes(self) -> None:
        list_action = parse_agent_action('{"action":"list_dir","path":"src","limit":5}')
        info_action = parse_agent_action('{"action":"file_info","path":"README.md"}')
        create_action = parse_agent_action('{"action":"create_file","path":"notes/new.txt","text":"hello"}')
        append_action = parse_agent_action('{"action":"append_file","path":"notes/new.txt","text":"again","create":false}')
        read_action = parse_agent_action('{"tool":"read","args":{"path":"README.md"}}')
        command_action = parse_agent_action(
            '{"tool_calls":[{"function":{"name":"run-command","arguments":"{\\"command\\":\\"python -V\\",\\"timeout_seconds\\":5}"}}]}'
        )
        transaction_action = parse_agent_action(
            '{"tool":"tool_transaction","arguments":{"steps":[{"action":"append_file","path":"notes/new.txt","text":" more"},{"action":"run_command","command":"python -V","timeout":7}]}}'
        )
        mcp_action = parse_agent_action(
            '{"action":"mcp_call","server":"fake","tool":"echo","arguments":{"text":"hi"},"timeout_seconds":3}'
        )
        mcp_tool_shape = parse_agent_action(
            '{"tool":"mcp_call","arguments":{"server":"fake","tool":"echo","arguments":{"text":"nested"}}}'
        )
        response_action = parse_agent_action(
            '{"actions":[{"name":"final_answer","arguments":{"message":"ready","reason":"done"}}]}'
        )

        self.assertEqual(list_action["action"], "list_dir")
        self.assertEqual(list_action["limit"], 5)
        self.assertEqual(info_action["action"], "file_info")
        self.assertEqual(create_action["action"], "create_file")
        self.assertEqual(create_action["text"], "hello")
        self.assertEqual(append_action["action"], "append_file")
        self.assertFalse(append_action["create"])
        self.assertEqual(read_action["action"], "read_file")
        self.assertEqual(read_action["path"], "README.md")
        self.assertEqual(command_action["action"], "run_command")
        self.assertEqual(command_action["command"], "python -V")
        self.assertEqual(command_action["timeout_seconds"], 5)
        self.assertEqual(transaction_action["action"], "transaction")
        self.assertEqual(len(transaction_action["steps"]), 2)
        self.assertEqual(transaction_action["steps"][0]["action"], "append_file")
        self.assertEqual(transaction_action["steps"][1]["timeout_seconds"], 7)
        self.assertEqual(mcp_action["action"], "mcp_call")
        self.assertEqual(mcp_action["server"], "fake")
        self.assertEqual(mcp_action["tool"], "echo")
        self.assertEqual(mcp_action["arguments"], {"text": "hi"})
        self.assertEqual(mcp_action["timeout_seconds"], 3)
        self.assertEqual(mcp_tool_shape["action"], "mcp_call")
        self.assertEqual(mcp_tool_shape["arguments"], {"text": "nested"})
        self.assertEqual(response_action["action"], "respond")
        self.assertEqual(response_action["message"], "ready")

    def test_setup_command_writes_project_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with patch("sys.stdout", new=io.StringIO()) as stdout:
                    main(
                        [
                            "setup",
                            "--api-key",
                            "sk-test123456789012345",
                            "--base-url",
                            "https://example.test",
                            "--model",
                            "gpt-5.5",
                            "--aux-model",
                            "gpt-5.3-codex",
                        ]
                    )
            finally:
                os.chdir(previous)

            values = parse_env_file(Path(tmp) / ".env")
            self.assertEqual(values["AKERNEL_OPENAI_API_KEY"], "sk-test123456789012345")
            self.assertEqual(values["AKERNEL_OPENAI_BASE_URL"], "https://example.test/v1")
            self.assertEqual(values["AKERNEL_OPENAI_MODEL"], "gpt-5.5")
            self.assertEqual(values["AKERNEL_OPENAI_AUX_MODEL"], "gpt-5.3-codex")
            self.assertEqual(values["AKERNEL_OPENAI_TIMEOUT_SECONDS"], "180")
            self.assertEqual(values["AKERNEL_OPENAI_MAX_RETRIES"], "3")
            self.assertEqual(values["AKERNEL_OPENAI_RETRY_BACKOFF_SECONDS"], "1.5")
            self.assertIn("api_key: set", stdout.getvalue())
            self.assertIn("auxiliary_model: gpt-5.3-codex", stdout.getvalue())
            self.assertIn("max_retries: 3", stdout.getvalue())

    def test_agent_loop_can_read_file_then_respond_with_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "plan.txt").write_text("ship tool planning next", encoding="utf-8")

            report = AgentLoop(workspace).run(
                "Read notes/plan.txt and tell me what it says.",
                provider_name="mock",
                budget=1200,
                max_steps=2,
                remember=True,
            )
            task = TaskStore(workspace).get(report["task_id"])
            brief = TaskStore(workspace).brief(report["task_id"])

            self.assertEqual(report["status"], "responded")
            self.assertEqual(len(report["steps"]), 2)
            self.assertEqual(report["steps"][0]["action"]["action"], "read_file")
            self.assertEqual(report["steps"][1]["action"]["action"], "respond")
            self.assertIn("ship tool planning next", report["final_response"])
            self.assertEqual(len(task["refs"]["tool_traces"]), 1)
            self.assertEqual(len(task["refs"]["run_traces"]), 2)
            self.assertEqual(len(task["refs"]["memories"]), 1)
            self.assertIn("ship tool planning next", brief["linked_tool_traces"][-1]["output_summary"])

    def test_agent_loop_can_patch_verify_and_respond(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "plan.txt").write_text("ship soon", encoding="utf-8")

            report = AgentLoop(workspace).run(
                'Patch notes/plan.txt replace "soon" with "now" and run command python -c "from pathlib import Path; print(Path(\'notes/plan.txt\').read_text(encoding=\'utf-8\'))"',
                provider_name="mock",
                budget=1400,
                max_steps=3,
                remember=True,
            )
            task = TaskStore(workspace).get(report["task_id"])
            brief = TaskStore(workspace).brief(report["task_id"])

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["patch_file", "run_command", "respond"])
            self.assertEqual((Path(tmp) / "notes" / "plan.txt").read_text(encoding="utf-8"), "ship now")
            self.assertIn("Verification command", report["final_response"])
            self.assertIn("ship now", report["final_response"])
            self.assertEqual(len(task["refs"]["tool_traces"]), 2)
            self.assertEqual(len(task["refs"]["run_traces"]), 3)
            self.assertEqual(len(task["refs"]["memories"]), 1)
            self.assertEqual(report["state"]["written_count"], 1)
            self.assertIn("ship now", brief["linked_tool_traces"][-1]["output_summary"])

    def test_agent_loop_recovers_from_patch_failure_with_structured_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "plan.txt").write_text("ship soon and soon", encoding="utf-8")

            report = AgentLoop(workspace).run(
                'Patch notes/plan.txt replace "soon" with "now" and run command python -c "from pathlib import Path; print(Path(\'notes/plan.txt\').read_text(encoding=\'utf-8\'))"',
                provider_name="mock",
                budget=2000,
                max_steps=4,
                remember=True,
            )
            task = TaskStore(workspace).get(report["task_id"])
            brief = TaskStore(workspace).brief(report["task_id"])

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["patch_file", "patch_file", "run_command", "respond"])
            self.assertEqual(report["steps"][0]["status"], "recovery_prepared")
            self.assertEqual(report["steps"][0]["recovery_tools"][0]["name"], "read_file")
            self.assertEqual(report["steps"][1]["action"]["replace_all"], True)
            self.assertEqual((Path(tmp) / "notes" / "plan.txt").read_text(encoding="utf-8"), "ship now and now")
            self.assertIn("Verification command", report["final_response"])
            self.assertEqual(len(task["refs"]["tool_traces"]), 4)
            self.assertEqual(len(task["refs"]["run_traces"]), 4)
            self.assertIn("ship now and now", brief["linked_tool_traces"][-1]["output_summary"])

    def test_agent_loop_can_patch_replace_all_without_rewrite_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "plan.txt").write_text("ship soon and soon", encoding="utf-8")

            report = AgentLoop(workspace).run(
                'Patch notes/plan.txt replace all "soon" with "now" and run command python -c "from pathlib import Path; print(Path(\'notes/plan.txt\').read_text(encoding=\'utf-8\'))"',
                provider_name="mock",
                budget=1600,
                max_steps=3,
                remember=True,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["patch_file", "run_command", "respond"])
            self.assertEqual(report["steps"][0]["action"]["replace_all"], True)
            self.assertEqual((Path(tmp) / "notes" / "plan.txt").read_text(encoding="utf-8"), "ship now and now")
            self.assertIn("ship now and now", report["final_response"])

    def test_agent_loop_can_patch_between_anchors_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "block.md").write_text(
                "alpha\n<!-- START -->\nold body\n<!-- END -->\nomega\n",
                encoding="utf-8",
            )

            report = AgentLoop(workspace).run(
                'Patch notes/block.md between "<!-- START -->" and "<!-- END -->" with "fresh body" and run command python -c "from pathlib import Path; print(Path(\'notes/block.md\').read_text(encoding=\'utf-8\'))"',
                provider_name="mock",
                budget=1800,
                max_steps=3,
                remember=True,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["patch_file", "run_command", "respond"])
            self.assertEqual(report["steps"][0]["action"]["start_anchor"], "<!-- START -->")
            self.assertEqual(report["steps"][0]["action"]["end_anchor"], "<!-- END -->")
            self.assertEqual(
                (Path(tmp) / "notes" / "block.md").read_text(encoding="utf-8"),
                "alpha\n<!-- START -->\nfresh body\n<!-- END -->\nomega\n",
            )
            self.assertIn("fresh body", report["final_response"])

    def test_agent_loop_can_batch_patch_multiple_files_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "a.txt").write_text("alpha old", encoding="utf-8")
            (Path(tmp) / "notes" / "b.txt").write_text("beta old", encoding="utf-8")

            report = AgentLoop(workspace).run(
                'Patch notes/a.txt replace "old" with "new"; patch notes/b.txt replace "old" with "new" and run command python -c "from pathlib import Path; print(Path(\'notes/a.txt\').read_text(encoding=\'utf-8\') + \'|\' + Path(\'notes/b.txt\').read_text(encoding=\'utf-8\'))"',
                provider_name="mock",
                budget=2200,
                max_steps=3,
                remember=True,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["batch_patch", "run_command", "respond"])
            self.assertEqual(report["steps"][0]["action"]["edit_count"], 2)
            self.assertEqual((Path(tmp) / "notes" / "a.txt").read_text(encoding="utf-8"), "alpha new")
            self.assertEqual((Path(tmp) / "notes" / "b.txt").read_text(encoding="utf-8"), "beta new")
            self.assertIn("alpha new|beta new", report["final_response"])

    def test_agent_loop_can_write_verify_and_respond(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            report = AgentLoop(workspace).run(
                'Write notes/new.txt with "hello agent" and run command python -c "from pathlib import Path; print(Path(\'notes/new.txt\').read_text(encoding=\'utf-8\'))"',
                provider_name="mock",
                budget=1600,
                max_steps=3,
                remember=True,
            )
            task = TaskStore(workspace).get(report["task_id"])
            brief = TaskStore(workspace).brief(report["task_id"])

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["write_file", "run_command", "respond"])
            self.assertEqual((Path(tmp) / "notes" / "new.txt").read_text(encoding="utf-8"), "hello agent")
            self.assertIn("Verification command", report["final_response"])
            self.assertIn("hello agent", report["final_response"])
            self.assertEqual(len(task["refs"]["tool_traces"]), 2)
            self.assertEqual(len(task["refs"]["run_traces"]), 3)
            self.assertEqual(len(task["refs"]["memories"]), 1)
            self.assertEqual(report["state"]["written_count"], 1)
            self.assertIn("hello agent", brief["linked_tool_traces"][-1]["output_summary"])

    def test_agent_loop_uses_create_and_append_file_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            create_report = AgentLoop(workspace).run(
                'Create notes/new.txt with "hello"',
                provider_name="mock",
                budget=1600,
                max_steps=2,
            )
            append_report = AgentLoop(workspace).run(
                'Append notes/new.txt with " again"',
                provider_name="mock",
                budget=1600,
                max_steps=2,
            )
            unquoted_create_report = AgentLoop(workspace).run(
                "Create notes/plain.txt with plain hello",
                provider_name="mock",
                budget=1600,
                max_steps=2,
            )
            unquoted_append_report = AgentLoop(workspace).run(
                "Append notes/plain.txt with plus one",
                provider_name="mock",
                budget=1600,
                max_steps=2,
            )

            self.assertEqual(create_report["status"], "responded")
            self.assertEqual(create_report["steps"][0]["action"]["action"], "create_file")
            self.assertEqual(append_report["status"], "responded")
            self.assertEqual(append_report["steps"][0]["action"]["action"], "append_file")
            self.assertEqual((Path(tmp) / "notes" / "new.txt").read_text(encoding="utf-8"), "hello again")
            self.assertEqual(unquoted_create_report["steps"][0]["action"]["action"], "create_file")
            self.assertEqual(unquoted_append_report["steps"][0]["action"]["action"], "append_file")
            self.assertEqual((Path(tmp) / "notes" / "plain.txt").read_text(encoding="utf-8"), "plain helloplus one")

    def test_agent_loop_can_list_directory_then_respond(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir()
            (Path(tmp) / "notes" / "a.txt").write_text("alpha", encoding="utf-8")

            report = AgentLoop(workspace).run(
                "List notes",
                provider_name="mock",
                budget=1600,
                max_steps=2,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual(report["steps"][0]["action"]["action"], "list_dir")
            self.assertIn("a.txt", report["final_response"])

    def test_agent_loop_materializes_code_block_response_to_explicit_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            action = {
                "action": "respond",
                "message": "Here is the file:\n```python\nprint('excel export ready')\n```",
                "reason": "done",
            }

            class LeakyCodeProvider:
                name = "leaky-code"
                model = "test"

                def run(self, packet):
                    return ProviderResponse(json.dumps(action), input_tokens=10, output_tokens=12)

            with patch("context_kernel.runner.get_provider", return_value=LeakyCodeProvider()):
                report = AgentLoop(workspace).run(
                    "Create financial_analyzer.py with Python code and export Excel.",
                    provider_name="openai",
                    budget=1800,
                    max_steps=1,
                    remember=False,
                )

            target = Path(tmp) / "financial_analyzer.py"
            self.assertEqual(report["status"], "responded")
            self.assertTrue(target.exists())
            self.assertIn("excel export ready", target.read_text(encoding="utf-8"))
            self.assertEqual(report["materialized_files"], [str(target)])
            self.assertIn("Wrote code to file", report["final_response"])

    def test_agent_loop_materializes_code_block_response_to_generated_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            action = {
                "action": "respond",
                "message": "Use this implementation:\n```python\nprint(123)\n```",
                "reason": "done",
            }

            class LeakyCodeProvider:
                name = "leaky-code"
                model = "test"

                def run(self, packet):
                    return ProviderResponse(json.dumps(action), input_tokens=10, output_tokens=12)

            with patch("context_kernel.runner.get_provider", return_value=LeakyCodeProvider()):
                events: list[dict[str, object]] = []
                report = AgentLoop(workspace).run(
                    "Create Python code that prints 123.",
                    provider_name="openai",
                    budget=1800,
                    max_steps=1,
                    remember=False,
                    progress_callback=events.append,
                )

            generated = list((Path(tmp) / "generated").glob("*.py"))
            self.assertEqual(report["status"], "responded")
            self.assertEqual(len(generated), 1)
            self.assertIn("print(123)", generated[0].read_text(encoding="utf-8"))
            self.assertEqual(report["materialized_files"], [str(generated[0])])
            self.assertTrue(any(event.get("event") == "materialize_start" for event in events))
            self.assertTrue(any(event.get("event") == "materialize_end" for event in events))

    def test_agent_loop_prepares_recovery_context_after_command_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            report = AgentLoop(workspace).run(
                'Write notes/new.txt with "hello agent" and run command python -c "import sys; sys.exit(1)"',
                provider_name="mock",
                budget=1800,
                max_steps=3,
                remember=True,
            )
            brief = TaskStore(workspace).brief(report["task_id"])

            self.assertEqual(report["status"], "responded")
            self.assertEqual([step["action"]["action"] for step in report["steps"]], ["write_file", "run_command", "respond"])
            self.assertEqual(report["steps"][1]["status"], "recovery_prepared")
            self.assertEqual(report["steps"][1]["recovery_tools"][0]["name"], "read_file")
            self.assertIn("still failed", report["final_response"])
            self.assertIn("hello agent", brief["linked_tool_traces"][-1]["output_summary"])

    def test_agent_loop_transaction_failure_rolls_back_and_reports_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "plan.txt").write_text("stable", encoding="utf-8")
            events: list[dict[str, object]] = []

            action = {
                "action": "transaction",
                "steps": [
                    {"action": "append_file", "path": "notes/plan.txt", "text": " changed"},
                    {"action": "create_file", "path": "notes/temp.txt", "text": "temporary"},
                    {"action": "run_command", "command": "python -c \"import sys; sys.exit(2)\""},
                ],
                "reason": "apply and verify atomically",
            }

            class TransactionProvider:
                name = "transaction-provider"
                model = "test"

                def run(self, packet):
                    return ProviderResponse(json.dumps(action), input_tokens=10, output_tokens=18)

            with patch("context_kernel.runner.get_provider", return_value=TransactionProvider()):
                report = AgentLoop(workspace).run(
                    "Append notes/plan.txt, create notes/temp.txt, then verify atomically.",
                    provider_name="openai",
                    budget=1800,
                    max_steps=1,
                    remember=True,
                    progress_callback=events.append,
                )

            self.assertEqual(report["status"], "needs_review")
            self.assertEqual(report["steps"][0]["status"], "needs_review")
            self.assertEqual(report["steps"][0]["action"]["action"], "transaction")
            self.assertEqual(report["steps"][0]["action"]["step_count"], 3)
            self.assertEqual(report["steps"][0]["tool"]["name"], "transaction")
            self.assertFalse(report["steps"][0]["tool"]["ok"])
            self.assertIn("rolled_back=True", report["steps"][0]["tool"]["summary"])
            self.assertEqual(report["steps"][0]["diagnostic"]["category"], "transaction_failed")
            self.assertIn("rolled back", report["steps"][0]["diagnostic"]["suggestion"])
            self.assertEqual((Path(tmp) / "notes" / "plan.txt").read_text(encoding="utf-8"), "stable")
            self.assertFalse((Path(tmp) / "notes" / "temp.txt").exists())

            start = next(event for event in events if event.get("event") == "action_start")
            end = next(event for event in events if event.get("event") == "action_end")
            self.assertEqual(start["action"], "transaction")
            self.assertIn("append notes/plan.txt", start["target"])
            self.assertIn("verify python", start["target"])
            self.assertEqual(end["action"], "transaction")
            self.assertFalse(end["ok"])
            self.assertIn("rolled_back=True", end["summary"])

            tui = format_tui_report(report)
            self.assertIn("Run failed: transaction_failed", tui)
            self.assertIn("Outcome: needs_review", tui)
            self.assertIn("rolled_back=True", tui)

    def test_agent_loop_blocks_repeated_identical_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            (Path(tmp) / "notes").mkdir(parents=True, exist_ok=True)
            (Path(tmp) / "notes" / "loop.txt").write_text("loop content", encoding="utf-8")

            def fake_run(*args: object, **kwargs: object) -> dict[str, object]:
                call_index = fake_run.calls + 1
                fake_run.calls = call_index
                return {
                    "id": f"trace{call_index:02d}",
                    "created_at": "2026-05-11T00:00:00+00:00",
                    "provider": "mock",
                    "model": None,
                    "request": str(args[0]) if args else "",
                    "task_id": kwargs.get("task_id"),
                    "resume": True,
                    "context_packet": {},
                    "response": {
                        "text": '{"action":"read_file","path":"notes/loop.txt","max_chars":2000}',
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                    "verifier": {"ok": True},
                    "state": {"enabled": False, "candidate_count": 0, "written_count": 0, "records": []},
                }

            fake_run.calls = 0  # type: ignore[attr-defined]

            with patch.object(AgentRunner, "run", side_effect=fake_run):
                report = AgentLoop(workspace).run(
                    "Read notes/loop.txt twice if needed.",
                    provider_name="mock",
                    budget=900,
                    max_steps=3,
                    remember=False,
                )

            task = TaskStore(workspace).get(report["task_id"])
            self.assertEqual(report["status"], "needs_review")
            self.assertEqual(len(report["steps"]), 3)
            self.assertEqual(report["steps"][0]["action"]["action"], "read_file")
            self.assertEqual(report["steps"][1]["action"]["action"], "read_file")
            self.assertEqual(report["steps"][2]["action"]["action"], "read_file")
            self.assertIsNone(report["steps"][2]["tool_trace_id"])
            self.assertEqual(len(task["refs"]["tool_traces"]), 2)

    def test_agent_loop_mock_provider_avoids_blocked_command_when_allowlist_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            report = AgentLoop(workspace).run(
                "Run command hostname and tell me the result.",
                provider_name="mock",
                budget=900,
                max_steps=3,
                remember=False,
            )

            self.assertEqual(report["status"], "responded")
            self.assertEqual(len(report["steps"]), 1)
            self.assertEqual(report["steps"][0]["action"]["action"], "respond")
            self.assertIn("outside the workspace allowlist", report["final_response"])

    def test_agent_loop_stops_on_policy_blocked_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            def fake_run(*args: object, **kwargs: object) -> dict[str, object]:
                return {
                    "id": "trace-policy",
                    "created_at": "2026-05-11T00:00:00+00:00",
                    "provider": "mock",
                    "model": None,
                    "request": str(args[0]) if args else "",
                    "task_id": kwargs.get("task_id"),
                    "resume": True,
                    "context_packet": {},
                    "response": {
                        "text": '{"action":"run_command","command":"hostname","timeout_seconds":30}',
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                    "verifier": {"ok": True},
                    "state": {"enabled": False, "candidate_count": 0, "written_count": 0, "records": []},
                }

            with patch.object(AgentRunner, "run", side_effect=fake_run):
                report = AgentLoop(workspace).run(
                    "Run command hostname and tell me the result.",
                    provider_name="mock",
                    budget=900,
                    max_steps=3,
                    remember=False,
                )

            self.assertEqual(report["status"], "blocked")
            self.assertEqual(len(report["steps"]), 1)
            self.assertEqual(report["steps"][0]["action"]["action"], "run_command")
            self.assertTrue(report["steps"][0]["tool"]["blocked"])
            self.assertEqual(report["steps"][0]["recovery_tools"], [])

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                print_agent_report(report)

            output = stdout.getvalue()
            self.assertIn("outcome: blocked - Agent loop stopped: the final tool action was blocked by policy.", output)
            self.assertIn("resume: adjust the requested path/command or project policy", output)
            self.assertIn("inspect: akernel task brief", output)

    def test_execution_planner_surfaces_policy_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            plan = ExecutionPlanner(workspace).plan("Delete .env and run git reset --hard", 900)

            self.assertTrue(plan["policy"]["requires_policy_check"])
            self.assertGreaterEqual(len(plan["policy"]["warnings"]), 2)

    def test_request_policy_recognizes_chinese_destructive_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            result = assess_request_policy(workspace, "删除 .env 并重置仓库")

            self.assertTrue(result["requires_policy_check"])
            self.assertGreaterEqual(len(result["warnings"]), 2)

    def test_runner_blocks_over_budget_before_provider_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            long_request = " ".join(["context"] * 1200)

            with self.assertRaises(RuntimeError):
                AgentRunner(workspace).run(long_request, provider_name="mock", budget=300)

            self.assertEqual(list(workspace.traces_dir.glob("*.json")), [])

    def test_runner_records_response_verifier_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            trace = AgentRunner(workspace).run(
                "Summarize the project goal",
                provider_name="mock",
                budget=900,
                expect_json=True,
            )
            verification = verify_trace(trace, expect_json=True)

            self.assertFalse(trace["verifier"]["ok"])
            self.assertFalse(trace["verifier"]["checks"]["valid_json_response"])
            self.assertFalse(verification["checks"]["valid_json_response"])

    def test_state_writer_persists_explicit_run_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()

            trace = AgentRunner(workspace).run(
                "Summarize the project goal",
                provider_name="mock",
                budget=900,
                remember=True,
            )
            memory = MemoryStore(workspace).all(kind="task_state")

            self.assertEqual(trace["state"]["written_count"], 1)
            self.assertEqual(len(memory), 1)
            self.assertIn(trace["id"], memory[0].text)

    def test_state_writer_extracts_marked_memory_and_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            trace = {
                "id": "trace123",
                "provider": "mock",
                "request": "Record decisions",
                "response": {
                    "text": "Decision: Keep the CLI-first release.\nFact: API key is sk-secret123456789.",
                    "total_tokens": 42,
                },
                "verifier": {"ok": True},
            }

            candidates = marker_candidates(trace)
            result = StateWriter(workspace).write_from_trace(trace)
            records = MemoryStore(workspace).all()

            self.assertEqual([candidate["kind"] for candidate in candidates], ["decision", "fact"])
            self.assertEqual(result["written_count"], 3)
            self.assertEqual(len(records), 3)
            self.assertTrue(any(record.kind == "decision" for record in records))
            self.assertNotIn("sk-secret", " ".join(record.text for record in records))
            self.assertIn("[REDACTED]", redact("api_key=sk-secret123456789"))

    def test_openai_message_builder_and_text_extraction(self) -> None:
        packet = {"request": "test", "budget": {"estimated_used": 12}}
        messages = build_messages(packet)
        response = {"choices": [{"message": {"content": "hello"}}]}
        openai_tool_call_response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps({"path": "notes/openai.txt", "text": "hello"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
        claude_tool_use_response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "I will edit the file."},
                            {
                                "type": "tool_use",
                                "name": "patch_file",
                                "input": {"path": "notes/openai.txt", "old": "hello", "new": "hi"},
                            },
                        ]
                    }
                }
            ]
        }
        responses_api_function_call = {
            "output": [
                {
                    "type": "function_call",
                    "name": "run_command",
                    "arguments": json.dumps({"command": "python -V", "timeout_seconds": 5}),
                }
            ]
        }

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Context packet", messages[1]["content"])
        self.assertEqual(extract_text(response), "hello")
        self.assertEqual(parse_agent_action(extract_text(openai_tool_call_response))["action"], "write_file")
        self.assertEqual(parse_agent_action(extract_text(claude_tool_use_response))["action"], "patch_file")
        self.assertEqual(parse_agent_action(extract_text(responses_api_function_call))["action"], "run_command")

    def test_provider_tool_call_golden_fixtures(self) -> None:
        fixtures_path = Path(__file__).parent / "fixtures" / "provider_tool_calls.json"
        fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))

        for fixture in fixtures:
            with self.subTest(fixture=fixture["name"]):
                if fixture.get("expected_extract_error"):
                    with self.assertRaisesRegex(ValueError, fixture["expected_extract_error"]):
                        extract_text(fixture["response"])
                    continue

                text = extract_text(fixture["response"])
                if "expected_text" in fixture:
                    self.assertEqual(text, fixture["expected_text"])
                    continue

                if fixture.get("expected_error"):
                    with self.assertRaisesRegex(ValueError, fixture["expected_error"]):
                        parse_agent_action(text)
                    continue

                action = parse_agent_action(text)
                self.assertEqual(action["action"], fixture["expected_action"])
                if fixture.get("expected_path"):
                    self.assertEqual(action["path"], fixture["expected_path"])
                if fixture.get("expected_command"):
                    self.assertEqual(action["command"], fixture["expected_command"])
                if fixture.get("expected_step_count"):
                    self.assertEqual(len(action["steps"]), fixture["expected_step_count"])

    def test_openai_provider_retries_transient_network_error(self) -> None:
        provider = OpenAICompatibleProvider(
            model="gpt-test",
            base_url="https://example.test/v1",
            api_key="sk-test123456789",
            timeout_seconds=1,
            max_retries=3,
            retry_backoff_seconds=0,
        )
        calls = {"count": 0}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}'

        def fake_urlopen(_request: object, timeout: int = 0) -> FakeResponse:
            calls["count"] += 1
            if calls["count"] <= 2:
                raise urllib.error.URLError("temporary disconnect")
            self.assertEqual(timeout, 1)
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = provider.run({"request": "hello"})

        self.assertEqual(response.text, "ok")
        self.assertEqual(calls["count"], 3)

    def test_openai_provider_reads_network_retry_env(self) -> None:
        previous = {
            "AKERNEL_OPENAI_TIMEOUT_SECONDS": os.environ.get("AKERNEL_OPENAI_TIMEOUT_SECONDS"),
            "AKERNEL_OPENAI_MAX_RETRIES": os.environ.get("AKERNEL_OPENAI_MAX_RETRIES"),
            "AKERNEL_OPENAI_RETRY_BACKOFF_SECONDS": os.environ.get("AKERNEL_OPENAI_RETRY_BACKOFF_SECONDS"),
        }
        try:
            os.environ["AKERNEL_OPENAI_TIMEOUT_SECONDS"] = "7"
            os.environ["AKERNEL_OPENAI_MAX_RETRIES"] = "5"
            os.environ["AKERNEL_OPENAI_RETRY_BACKOFF_SECONDS"] = "0.25"
            provider = OpenAICompatibleProvider(
                model="gpt-test",
                base_url="https://example.test/v1",
                api_key="sk-test123456789",
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(provider.timeout_seconds, 7)
        self.assertEqual(provider.max_retries, 5)
        self.assertEqual(provider.retry_backoff_seconds, 0.25)

    def test_project_env_parser_and_base_url_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "AKERNEL_OPENAI_BASE_URL=https://clarmy.cloud/v1\n"
                "AKERNEL_OPENAI_MODEL='gpt-5.5'\n",
                encoding="utf-8",
            )

            self.assertEqual(parse_env_file(path)["AKERNEL_OPENAI_MODEL"], "gpt-5.5")
            self.assertEqual(normalize_openai_base_url("https://clarmy.cloud"), "https://clarmy.cloud/v1")
            self.assertEqual(normalize_openai_base_url("https://clarmy.cloud/v1"), "https://clarmy.cloud/v1")

    def test_project_root_env_is_fallback_when_running_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as work_tmp:
            root = Path(root_tmp)
            (root / ".env").write_text("CONTEXT_KERNEL_TEST_VALUE=from-project-root\n", encoding="utf-8")
            previous = Path.cwd()
            previous_root = os.environ.get("AKERNEL_PROJECT_ROOT")
            os.chdir(work_tmp)
            os.environ["AKERNEL_PROJECT_ROOT"] = str(root)
            try:
                self.assertEqual(env_value("CONTEXT_KERNEL_TEST_VALUE"), "from-project-root")
            finally:
                os.chdir(previous)
                if previous_root is None:
                    os.environ.pop("AKERNEL_PROJECT_ROOT", None)
                else:
                    os.environ["AKERNEL_PROJECT_ROOT"] = previous_root

    def test_eval_runner_can_execute_with_mock_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = EvalRunner(workspace)

            report = runner.run_fixture(
                ROOT / "examples" / "evals" / "phase2.json",
                save=False,
                execute_provider="mock",
            )

            self.assertEqual(report["summary"]["executed_tasks"], 2)
            self.assertGreater(report["summary"]["total_execution_tokens"], 0)
            self.assertIn("execution", report["tasks"][0])

    def test_eval_execution_blocks_over_budget_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            fixture = Path(tmp) / "over_budget.json"
            Workspace.write_json(
                fixture,
                {
                    "name": "Over Budget",
                    "tasks": [
                        {
                            "id": "too-large",
                            "request": " ".join(["context"] * 1200),
                            "budget": 300,
                        }
                    ],
                },
            )

            report = EvalRunner(workspace).run_fixture(fixture, execute_provider="mock", save=False)

            self.assertEqual(report["summary"]["executed_tasks"], 0)
            self.assertEqual(report["summary"]["blocked_tasks"], 1)
            self.assertTrue(report["tasks"][0]["execution"]["blocked"])
            self.assertEqual(report["summary"]["total_execution_tokens"], 0)

    def test_eval_cost_report_surfaces_hotspots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = EvalRunner(workspace)

            report = runner.run_fixture(
                ROOT / "examples" / "evals" / "phase2.json",
                save=False,
                execute_provider="mock",
            )
            cost = build_eval_cost_report(report)
            rendered = render_cost_report(cost)

            self.assertEqual(cost["kind"], "eval")
            self.assertEqual(cost["summary"]["item_count"], len(report["tasks"]))
            self.assertEqual(cost["summary"]["kernel_tokens"], report["summary"]["total_kernel_tokens"])
            self.assertGreaterEqual(cost["hotspots"][0]["kernel_tokens"], cost["hotspots"][-1]["kernel_tokens"])
            self.assertIn("hotspot:", rendered)
            self.assertIn("Lowest Savings", rendered)

    def test_benchmark_runner_aggregates_fixture_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            registry = SkillRegistry(workspace)
            registry.register(ROOT / "examples" / "skills" / "edit_file.json")
            registry.register(ROOT / "examples" / "skills" / "context_budget.json")
            memory = MemoryStore(workspace)
            memory.add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            memory.add("fact", "Extra unrelated memory increases baseline context.", ["noise"])
            runner = BenchmarkRunner(workspace)

            report = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")

            self.assertEqual(report["summary"]["fixture_count"], 3)
            self.assertEqual(report["summary"]["task_count"], 6)
            self.assertEqual(report["summary"]["passed_checks"], report["summary"]["total_checks"])
            self.assertGreater(report["summary"]["total_savings_tokens"], 0)
            self.assertTrue((workspace.benchmarks_dir / f"{report['id']}.json").exists())
            self.assertEqual(runner.list_reports()[0]["id"], report["id"])
            self.assertEqual(runner.get_report(report["id"])["id"], report["id"])

    def test_benchmark_diff_and_markdown_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            registry = SkillRegistry(workspace)
            registry.register(ROOT / "examples" / "skills" / "edit_file.json")
            registry.register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = BenchmarkRunner(workspace)
            before = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")
            MemoryStore(workspace).add("fact", "Extra unrelated memory increases baseline context.", ["noise"])
            after = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")

            diff = runner.diff_reports(before["id"], after["id"])
            cost = build_benchmark_cost_report(after)
            markdown_path = runner.export_markdown(after["id"])
            markdown = render_benchmark_markdown(after)

            self.assertIn("summary_delta", diff)
            self.assertIn("cost_diff", diff)
            self.assertEqual(cost["kind"], "benchmark")
            self.assertEqual(cost["summary"]["item_count"], after["summary"]["task_count"])
            self.assertEqual(len(diff["fixtures"]), 3)
            self.assertGreater(len(diff["cost_regressions"]), 0)
            self.assertTrue(markdown_path.exists())
            self.assertIn("# Benchmark Report", markdown)
            self.assertIn("## Cost View", markdown)
            self.assertIn("### Hotspots", markdown)

    def test_benchmark_evidence_summarizes_saved_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = BenchmarkRunner(workspace)
            first = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")
            MemoryStore(workspace).add("fact", "Extra unrelated memory increases baseline context.", ["noise"])
            second = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")

            evidence = runner.evidence([first["id"], second["id"]])
            markdown = render_benchmark_evidence_markdown(evidence)
            output = runner.export_evidence_markdown([first["id"], second["id"]])

            self.assertEqual(evidence["report_count"], 2)
            self.assertGreater(evidence["task_count"], first["summary"]["task_count"])
            self.assertGreater(evidence["total_savings_tokens"], 0)
            self.assertEqual(evidence["passed_checks"], evidence["total_checks"])
            self.assertTrue(evidence["strongest_savings"])
            self.assertTrue(evidence["weakest_savings"])
            self.assertIn("# Benchmark Evidence", markdown)
            self.assertTrue(output.exists())

    def test_benchmark_evidence_cli_can_fail_under_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            report = BenchmarkRunner(workspace).run_directory(ROOT / "examples" / "benchmarks" / "phase2")

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(["--workspace", str(workspace.root), "bench", "evidence", report["id"]])

            self.assertIn("Benchmark Evidence", stdout.getvalue())
            with patch("sys.stdout", new=io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "bench",
                            "evidence",
                            report["id"],
                            "--fail-under",
                            "99",
                        ]
                    )
            self.assertIn("benchmark evidence below threshold", str(exc.exception))

    def test_benchmark_runner_finds_latest_baseline_by_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = BenchmarkRunner(workspace)
            first = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")
            second = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")

            baseline = runner.find_baseline(ROOT / "examples" / "benchmarks" / "phase2", exclude_id=second["id"])

            self.assertIsNotNone(baseline)
            self.assertEqual(baseline["match"], "path")
            self.assertEqual(baseline["report"]["id"], first["id"])

    def test_benchmark_diff_can_fail_on_regression_for_cli_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = BenchmarkRunner(workspace)
            before = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")
            MemoryStore(workspace).add("fact", "Extra unrelated memory increases baseline context.", ["noise"])
            after = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")

            with patch("sys.stdout", new=io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "bench",
                            "diff",
                            before["id"],
                            after["id"],
                            "--fail-on-regression",
                        ]
                    )

            self.assertIn("benchmark diff found regressions", str(exc.exception))

    def test_benchmark_gate_uses_latest_matching_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = BenchmarkRunner(workspace)
            baseline = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "bench",
                        "gate",
                        str(ROOT / "examples" / "benchmarks" / "phase2"),
                    ]
                )

            output = stdout.getvalue()
            reports = list(workspace.benchmarks_dir.glob("*.json"))
            self.assertEqual(len(reports), 2)
            self.assertIn(f"baseline: {baseline['id']}", output)
            self.assertIn("baseline_match: path", output)
            self.assertIn("status: passed", output)
            self.assertIn("current_checks: 16/16", output)

    def test_benchmark_gate_fails_when_current_checks_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                with self.assertRaises(SystemExit) as exc:
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "bench",
                            "gate",
                            str(ROOT / "examples" / "benchmarks" / "phase2"),
                        ]
                    )

            self.assertIn("benchmark gate current benchmark checks failed", str(exc.exception))
            self.assertIn("status: failed", stdout.getvalue())

    def test_benchmark_gate_can_fail_on_cost_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])
            runner = BenchmarkRunner(workspace)
            baseline = runner.run_directory(ROOT / "examples" / "benchmarks" / "phase2")
            MemoryStore(workspace).add("fact", "Extra unrelated memory increases baseline context.", ["noise"])

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                with self.assertRaises(SystemExit) as exc:
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "bench",
                            "gate",
                            str(ROOT / "examples" / "benchmarks" / "phase2"),
                            "--baseline-report",
                            baseline["id"],
                        ]
                    )

            self.assertIn("benchmark gate found regressions", str(exc.exception))
            self.assertIn("status: failed", stdout.getvalue())

    def test_benchmark_gate_can_require_existing_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "edit_file.json")
            SkillRegistry(workspace).register(ROOT / "examples" / "skills" / "context_budget.json")
            MemoryStore(workspace).add("preference", "Prefer CLI-first context budget prototypes.", ["cli"])

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                with self.assertRaises(SystemExit) as exc:
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "bench",
                            "gate",
                            str(ROOT / "examples" / "benchmarks" / "phase2"),
                            "--require-baseline",
                        ]
                    )

            self.assertIn("benchmark gate could not find baseline", str(exc.exception))
            self.assertIn("status: missing_baseline", stdout.getvalue())

    def test_markdown_skill_compiler_outputs_registerable_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            workspace.init()
            source = ROOT / "examples" / "skills" / "markdown" / "context_budget.md"
            output = Path(tmp) / "context_budget.json"
            skill = compile_markdown_skill(source)
            Workspace.write_json(output, skill.to_dict())

            registered = SkillRegistry(workspace).register(output)
            validation = validate_skill_file(output)
            inspection = inspect_skill(registered, budget=120)

            self.assertEqual(skill.id, "context_budget")
            self.assertEqual(registered.id, "context_budget")
            self.assertTrue(validation["ok"])
            self.assertIn(inspection["selected_level"], {"l0", "l1", "l2", "l3"})

    def test_extract_json_object_accepts_fenced_provider_output(self) -> None:
        text = 'Here is JSON:\n```json\n{"id":"x","name":"X"}\n```'

        self.assertEqual(extract_json_object(text)["id"], "x")


if __name__ == "__main__":
    unittest.main()
