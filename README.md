# Context Kernel

[![CI](https://github.com/huanxin0825-ctrl/context-akernel/actions/workflows/ci.yml/badge.svg)](https://github.com/huanxin0825-ctrl/context-akernel/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/akernel-runtime.svg)](https://pypi.org/project/akernel-runtime/)
[![npm](https://img.shields.io/npm/v/@context-akernel/akernel.svg)](https://www.npmjs.com/package/@context-akernel/akernel)
[![License](https://img.shields.io/github/license/huanxin0825-ctrl/context-akernel.svg)](LICENSE)

**Context Kernel is a context-native agent runtime for building cheaper, sharper, and more controllable AI agents.**

Most agent tools still treat the prompt as the runtime: put the system rules, memory, skills, tool manuals, safety policy, chat history, and task state into one huge message, then ask the model to sort it out. That works, but it gets expensive, noisy, and fragile as the agent grows.

Context Kernel takes a different position:

> The prompt should be a small working set, not a warehouse.

`akernel` assembles only the context needed for the current step from structured memory, scoped skill contracts, project profiles, MCP/tool summaries, policy checks, and token budgets. Every inclusion is explainable. Every run is traced. Every token-saving claim can be benchmarked.

The project is currently an alpha CLI release, but the core idea is already usable: a local agent shell with OpenAI-compatible models, primary/auxiliary model roles, policy-gated tools, MCP and skill support, structured memory, benchmark evidence, and release-ready PyPI/npm distribution.

## Why Star This Project?

- **It attacks the real bottleneck under agent systems:** context explosion, not just UI polish.
- **It makes token usage auditable:** budgets, traces, cost reports, eval diffs, and benchmark gates are first-class runtime objects.
- **It keeps memory out of the prompt by default:** memory is typed, searchable state, not an endlessly compressed transcript.
- **It treats skills as contracts:** load the minimum useful skill layer instead of dumping full manuals into every call.
- **It is provider-flexible:** any OpenAI-compatible `/v1` endpoint can be used through project-local configuration.
- **It is built for serious local work:** file edits, safe commands, MCP calls, task checkpoints, and traceable recovery are part of the CLI loop.

If you believe future agents need operating-system-like runtime layers instead of ever-larger prompts, this project is exploring that path.

## The Problem

Modern agent workflows often become expensive for reasons that are hard to see:

- Large skill packs are loaded even when a task is simple.
- Long conversation history is repeatedly compressed, replayed, and reinterpreted.
- Memory is mixed with chat transcript instead of stored as typed state.
- Tool instructions and safety rules are duplicated across every model call.
- Agents spend expensive model tokens rediscovering project structure.
- Token regressions are noticed only after costs have already climbed.

The result is a narrowing loop: the more capable an agent becomes, the more scaffolding it carries, and the more context is spent before the model even starts reasoning.

Context Kernel moves those responsibilities into a runtime layer that can be inspected, tested, versioned, and improved.

## What Makes It Different

Context Kernel is not just another chat wrapper. It is a runtime architecture for context discipline.

| Common agent pattern | What usually happens | Context Kernel approach |
| --- | --- | --- |
| Long system prompts | Rules, tools, memory, and procedures accumulate forever. | Build a compact context packet for each step. |
| Chat-history memory | The transcript becomes the database. | Store typed memory records with relevance gates and pruning. |
| Full skill loading | A task gets entire skill documents even when it needs one constraint. | Use progressive skill contracts: summary, capability, constraints, then full procedure only when justified. |
| Tool prompts | Tool rules live mostly as natural-language instructions. | Route tool use through policy-gated runtime actions and saved traces. |
| Hidden token costs | Users discover cost regressions after the fact. | Estimate budgets before calls and generate cost reports after runs. |
| One-shot agent loops | Failure often means vague retry prompts. | Save run traces, tool traces, recovery context, and task checkpoints. |
| Provider lock-in | Agent behavior is tied to one hosted model surface. | Use an OpenAI-compatible provider interface with project-local `.env` config. |

This does not replace strong models. It helps strong models spend less attention on boilerplate and more attention on the actual task.

## Core Capabilities

- **Interactive agent CLI:** run `akernel` from any directory and start a focused agent workspace.
- **Primary + auxiliary model stack:** use a strong primary model for execution and an auxiliary model for cheaper planning or review.
- **Structured memory:** typed records backed by local SQLite and recoverable traces.
- **Progressive skill contracts:** load only the level of a skill that the task needs.
- **MCP integration:** register, refresh, enable, disable, inspect, and call MCP tools from the CLI or chat.
- **Policy-gated tools:** keep file and command execution behind explicit runtime checks.
- **Stable file actions:** support `list_dir`, `file_info`, `read_file`, `create_file`, `write_file`, `append_file`, `patch_file`, `batch_patch`, `run_command`, `mcp_call`, and `respond`.
- **Code materialization guard:** code-writing tasks are steered into workspace files instead of being left as chat-only code blocks.
- **Resumable task planning:** keep long work in milestones and compact checkpoints instead of replaying the whole conversation.
- **Token budgets and cost reports:** estimate context pressure before provider calls and report actual run costs afterward.
- **Benchmark and eval gates:** compare behavior and token-cost regressions with reproducible local fixtures.
- **PyPI + npm distribution:** install as `akernel-runtime` or launch through `@context-akernel/akernel`.

## Benchmark Evidence

Context Kernel includes deterministic benchmark fixtures that compare its compact context packet against a full-load baseline.

Current reproducible scale snapshot:

| Metric | Result |
| --- | ---: |
| Fixtures | `3` |
| Tasks | `6` |
| Checks | `12/12` |
| Pass rate | `100.0%` |
| Kernel tokens | `1235` |
| Full-load baseline tokens | `2447` |
| Total savings | `49.53%` |

These numbers come from the local mock-provider benchmark path, so they are reproducible without external API keys. They are not a final production-wide claim; they are the proof mechanism. The goal is to make every token-saving improvement measurable rather than vibes-based.

See [Benchmark Evidence](docs/10-benchmark-evidence.md) for reproduction commands and fixture details.

## Architecture

```text
user request
  -> intent router
  -> project profile
  -> context budgeter
  -> memory retriever
  -> skill contract selector
  -> MCP/tool summary selector
  -> provider adapter
  -> response verifier
  -> policy-gated tool executor
  -> trace store
  -> cost report
```

The guiding rule is simple: every context inclusion should be explainable before the model runs, and every optimization should have a trace or benchmark behind it.

## Naming

`akernel` means **Agent Kernel**. The project name is Context Kernel because the runtime focuses on context assembly, memory, policy, skills, tools, and token budgets; the executable is `akernel` because users invoke the agent-facing kernel directly from the terminal.

The shorter `kernel` command is intentionally avoided because it is too broad and already carries strong meanings in operating systems, Jupyter, and other AI projects. `akernel` keeps the kernel idea while giving the CLI a distinct, searchable identity.

Package and command names map to the same idea:

- GitHub repository: `context-akernel`
- Python distribution: `akernel-runtime`
- Python import package: `context_kernel`
- CLI command: `akernel`
- npm launcher: `@context-akernel/akernel`

## Install

### Windows One-Command Setup

```powershell
git clone https://github.com/huanxin0825-ctrl/context-akernel.git
cd context-akernel
.\setup.cmd
akernel setup
akernel
```

`setup.cmd` creates `.venv`, installs the local CLI in editable mode, and prepares project-local `.env` if needed. `wake.cmd` activates the environment, loads `.env`, and prints common commands. The `.cmd` wrappers avoid local PowerShell execution-policy friction.

`setup.cmd` also installs user-level launchers in `%USERPROFILE%\.akernel\bin` and adds that directory to the user PATH. After opening a new terminal, `akernel` works from any directory, starts the interactive agent session by default, and uses that current directory as the workspace location. Environment lookup prefers the current project `.env`, then falls back to the installed Context Kernel project `.env`. `akernel-chat` remains as a compatibility shortcut.

### Manual Python Install

```powershell
python -m pip install -e .[dev]
akernel --help
```

Python 3.10 or newer is required.

### PyPI Install

The Python runtime is published on PyPI as `akernel-runtime`:

```powershell
python -m pip install --user akernel-runtime
akernel setup
akernel
```

### GitHub Remote Install

Windows users can also install directly from GitHub:

```powershell
irm https://raw.githubusercontent.com/huanxin0825-ctrl/context-akernel/main/scripts/install_remote.ps1 | iex
akernel setup
akernel
```

### npm Launcher

The npm launcher is published as `@context-akernel/akernel`. It provides a Node-style entrypoint and bootstraps or upgrades the Python package if it is missing or stale:

```powershell
npm install -g @context-akernel/akernel
akernel setup
akernel
```

Set `AKERNEL_PIP_SOURCE=git+https://github.com/huanxin0825-ctrl/context-akernel.git` to make the npm launcher bootstrap from GitHub instead of PyPI.

## Quick Start

```powershell
akernel setup
akernel init . --scan
akernel
akernel --ui tui
akernel --provider mock
akernel tool create notes\result.txt --text "hello"
akernel tool append notes\result.txt --text " world"
akernel tool list-dir notes
akernel tool file-info notes\result.txt
```

Inside the interactive session, type a task and press Enter. Bare `akernel` accepts chat flags such as `--provider mock`, `--model`, `--aux-model`, `--max-steps`, and `--ui tui` without requiring the explicit `chat` subcommand. `--ui auto` now stays in the calm scrollback-friendly chat by default; use `--ui tui` or `AKERNEL_UI=tui` when you explicitly want the richer terminal layout. `akernel init . --scan` or `akernel project scan` writes a compact `.akernel/project.json` profile with detected languages, package managers, key files, project instruction files such as `AGENTS.md`, and test/build commands; this profile enters future context packets without loading the whole repository. If you ask to `run tests` or `verify` without naming a command, the agent prefers the scanned project test command. If you ask it to fix failing tests, the agent can run that profile test command, inspect one or more failing files from the command output, apply a bounded patch or rollback-safe batch patch when the failure is simple enough, and rerun verification.

The interactive chat UI opens quietly: workspace path, task id, provider/model/profile, and a short command hint line. Detailed session cards live behind `/status`, `/model`, `/extensions`, `/compact`, `/runs`, and `/cost`, so the default prompt stays comfortable for daily use. The optional TUI mode keeps a fixed header, command strip, transcript viewport, Focus/Flow sidebar, Task panel, Last Run summary, diagnostics, and bottom input hints without adding runtime dependencies. Type `/status` for the live workspace runway, `/model` for primary and auxiliary model roles, `/extensions` for MCP and skill availability, `/compact` for the task brief, `/cost` for the last run's token report, `/task` to inspect the current task session, and `/exit` to leave. Type `/` for command completion, `@` for workspace file completion, or mention an exact `@path` inside a task to attach that file automatically. Project and user slash commands can be saved as Markdown prompts under `.akernel/commands` or `~/.akernel/commands`; use `/commands` to list them. In `--model-routing auto` mode, low/medium first-step planning can run on the auxiliary model while high-risk, deep, warning-heavy, or synthesis steps stay on the primary model. In `--aux-review auto` mode, auxiliary review runs before primary-model steps and is included in token cost reports.

If you want to prepare the benchmark workspace manually:

```powershell
akernel --workspace .sandbox skill register examples\skills\edit_file.json
akernel --workspace .sandbox skill register examples\skills\context_budget.json
akernel --workspace .sandbox memory add --kind preference --text "Prefer CLI-first context budget prototypes." --tags cli
akernel --workspace .sandbox plan "Plan a CLI context budget prototype"
```

Run the benchmark suite and gate it against the latest matching baseline:

```powershell
akernel --workspace .sandbox bench run examples\benchmarks\phase2
akernel --workspace .sandbox bench gate examples\benchmarks\phase2 --require-baseline
akernel --workspace .sandbox bench run examples\benchmarks\scale
akernel --workspace .sandbox bench evidence --limit 3 --fail-under 30 --output .sandbox\benchmark-evidence.md
```

`bench gate` requires the current benchmark checks to pass, then compares behavior and token cost against a saved baseline.
`bench evidence` turns saved benchmark reports into a Markdown proof page with total kernel tokens, baseline tokens, token savings, pass rate, strongest savings, and weakest savings.
The current deterministic scale snapshot is documented in [Benchmark Evidence](docs/10-benchmark-evidence.md).

## OpenAI-Compatible Provider

Provider configuration is project-local. Copy `.env.example` to `.env` or use `setup.cmd -ForceEnv`.

```env
AKERNEL_OPENAI_API_KEY=replace-with-your-key
AKERNEL_OPENAI_BASE_URL=https://clarmy.cloud/v1
AKERNEL_OPENAI_MODEL=gpt-5.5
AKERNEL_OPENAI_AUX_MODEL=gpt-5.3-codex
AKERNEL_OPENAI_TIMEOUT_SECONDS=180
AKERNEL_OPENAI_MAX_RETRIES=3
AKERNEL_OPENAI_RETRY_BACKOFF_SECONDS=1.5
```

Legacy `CONTEXT_KERNEL_OPENAI_*` names are still read as a compatibility fallback, but new projects should use `AKERNEL_OPENAI_*`.

Useful checks:

```powershell
akernel doctor
akernel models --provider openai
akernel --workspace .sandbox run "Reply with exactly OK." --provider openai --model gpt-5.5 --profile lean
akernel --workspace .sandbox run "Reply with exactly OK." --provider openai --model gpt-5.3-codex --profile lean
```

The base URL should include `/v1`.

## CLI Highlights

```powershell
akernel
akernel --workspace .sandbox chat
akernel context "Continue this task" --task <task-id> --resume
akernel task start "Ship a complex feature" --goal "Plan, implement, verify, and document the feature" --plan
akernel task next <task-id>
akernel task checkpoint <task-id> --milestone M1 --status completed --note "Investigation complete"
akernel project scan
akernel project show
akernel memory audit
akernel memory prune --max-records 100 --dry-run
akernel memory global-push
akernel memory global-pull --limit 20
akernel memory global-push --namespace team-runtime --tag shared --dry-run
akernel memory global-pull --namespace team-runtime --source-project my-project --dry-run
akernel skill market-list
akernel skill market-list --index examples\marketplace\skills\index.json
akernel skill market-install multi_file_bugfix
akernel skill market-install remote_skill --index https://example.com/context-kernel/skills/index.json --trust-remote
akernel mcp import-codex
akernel mcp add filesystem --command "python -m mcp_server_filesystem ." --tool "read_file:Read workspace files"
akernel mcp list
akernel mcp refresh filesystem
akernel mcp call filesystem read_file --args "{\"path\":\"README.md\"}"
akernel mcp disable filesystem
akernel mcp enable filesystem
akernel mcp remove filesystem
akernel compare "Summarize the project goal"
akernel eval run examples\evals\phase2.json
akernel eval cost <report-id>
akernel eval diff <before-id> <after-id> --fail-on-regression
akernel bench cost <report-id>
akernel bench diff <before-id> <after-id> --fail-on-regression
akernel bench evidence --limit 3 --fail-under 30 --output benchmark-evidence.md
akernel agent run "Patch notes/plan.txt and run tests" --provider openai --max-steps 4
akernel agent cost <agent-run-id>
```

Inside `akernel`, type a natural-language task and press Enter. Use `/extensions` to inspect MCP servers and registered skills, `/mcp` or `/skills` for focused extension panels, `/mcp refresh <name>` and `/mcp call <server> <tool> --args "{...}"` for MCP operations without leaving chat, `/cost` for the last run's token report, `/task` to inspect the current task session, and `/exit` to leave.

Skill workflows are also available inside chat: `/skills recommend <task>` ranks registered skills for the next task, `/skills show <id>` previews a skill contract, and `/skills install <marketplace-id>` installs a packaged skill without leaving the session.

By default, chat and agent runs route work to the configured primary model. Use `--model-routing auto` only when you intentionally want cost-saving auxiliary first-step routing. While work is running, the CLI prints live step status such as selected model role, model name, action, and token count.

Interactive runs show a live spinner while waiting for provider responses. Set `AKERNEL_NO_SPINNER=1` to disable it, or `AKERNEL_SPINNER=1` to force it in non-interactive terminals.

When no explicit `--budget` is provided, chat and agent runs can automatically expand the per-turn context budget if the compact task state grows beyond the conservative default. Explicit budgets remain hard limits.

MCP v1 stores local stdio server configuration in `.akernel/mcp.json`. Enabled MCP servers enter the context packet as compact summaries only: server name, transport, command root, startup metadata, env key names, approval hints, and curated tool summaries. `akernel mcp import-codex` imports `[mcp_servers.*]` entries from `~/.codex/config.toml`, preserving command args, startup timeouts, and per-tool approval metadata. For safety it does not copy env values by default; servers that require env values are imported disabled unless you explicitly pass `--include-env`. `akernel mcp refresh <name>` starts a stdio MCP server, runs `initialize` and `tools/list`, and stores the discovered tool summaries. `akernel mcp call <name> <tool>` manually invokes a discovered MCP tool and records the result as a tool trace. Agent runs can also choose `mcp_call` automatically, but only for enabled servers and discovered tools listed in the current context packet.

When an agent run cannot continue, the CLI prints a compact diagnostic with a category, reason, and suggested next step. Common categories include provider configuration, provider auth/network/protocol errors, context budget blocks, policy blocks, command failures, malformed provider actions, and loop guards.

See [docs/03-cli-mvp.md](docs/03-cli-mvp.md) for the full command surface.

## Development

```powershell
python -m pip install -e .[dev]
python -m unittest discover -s tests -p test_runtime.py
python -m build
```

The repository CI runs unit tests, package build checks, CLI smoke tests, benchmark regression gates, and the Windows setup/wake flow. See [docs/07-release-and-ci.md](docs/07-release-and-ci.md).

## Documentation

- [Vision](docs/00-vision.md)
- [Architecture](docs/01-architecture.md)
- [Execution Plan](docs/02-execution-plan.md)
- [CLI MVP](docs/03-cli-mvp.md)
- [Evaluation Strategy](docs/04-evaluation.md)
- [Local Wake Workflow](docs/05-local-wake.md)
- [Skill Compiler](docs/06-skill-compiler.md)
- [Release And CI](docs/07-release-and-ci.md)
- [Open Source Plan](docs/08-open-source-plan.md)
- [Product Roadmap](docs/09-product-roadmap.md)
- [Benchmark Evidence](docs/10-benchmark-evidence.md)
- [Publishing Setup](docs/11-publishing-setup.md)

## Project Status

Context Kernel is alpha software. The CLI is functional, tested, and benchmarked locally, but the public API and file formats may still change as the runtime model matures.

Good first contribution areas:

- new eval fixtures for real coding and research workflows
- better routing and scoring strategies
- richer token cost visualization
- provider adapters for more OpenAI-compatible endpoints
- documentation improvements from fresh-user setup attempts

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md), keep changes small and benchmarkable, and include tests for behavior that affects routing, budgeting, policy, tools, or reports.

## Security

Do not commit `.env`, API keys, provider responses containing secrets, or local `.akernel` state. See [SECURITY.md](SECURITY.md) for reporting guidance.

## License

Apache License 2.0. See [LICENSE](LICENSE).
