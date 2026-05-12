# Context Kernel

Context Kernel is a CLI-first agent runtime for reducing prompt bloat, memory drift, and unnecessary token spending.

Most agent systems treat the prompt as the runtime. Context Kernel treats the prompt as a small working set assembled from structured state, scoped skill contracts, explicit policy checks, and measurable token budgets.

The project is currently an alpha CLI release. It is ready for local experimentation, benchmark-driven iteration, and early contributor feedback.

## Why This Exists

Modern agent workflows often become expensive for reasons that are hard to see:

- large skill packs are loaded even when a task is simple
- long conversation history is repeatedly compressed and replayed
- memory is mixed with chat transcript instead of stored as typed state
- tool instructions and safety rules are duplicated across every call
- token regressions are noticed only after costs have already climbed

Context Kernel is an experiment in moving those responsibilities into a runtime layer that can be inspected, tested, and improved.

## Core Capabilities

- Structured memory: typed records backed by local SQLite and JSONL state.
- Progressive skill contracts: load only the level of a skill that the task needs.
- Token budgets: estimate and report context pressure before provider calls.
- Resumable task planning: keep long work in structured milestones and compact checkpoints instead of replaying chat history.
- Bounded agent loop: support `read_file`, `write_file`, `patch_file`, `batch_patch`, `run_command`, and `respond` actions.
- Policy-gated tools: keep file and command execution behind explicit runtime checks.
- Traceability: write run traces, tool traces, compact agent reports, and token cost reports.
- Regression gates: compare evals and benchmarks, including behavior and token cost regressions.
- OpenAI-compatible provider: use project-local `.env` config with any compatible `/v1` endpoint.

## Architecture

```text
request
  -> router
  -> budgeter
  -> memory retriever
  -> skill contract loader
  -> provider adapter
  -> verifier
  -> state writer
  -> trace and cost reports
```

The guiding rule is simple: every context inclusion should be explainable before the model runs, and every optimization should have a trace or benchmark behind it.

## Install

### Windows One-Command Setup

```powershell
git clone <repository-url>
cd context-kernel
.\setup.cmd
akernel setup
akernel
```

`setup.cmd` creates `.venv`, installs the local CLI in editable mode, and prepares project-local `.env` if needed. `wake.cmd` activates the environment, loads `.env`, and prints common commands. The `.cmd` wrappers avoid local PowerShell execution-policy friction.

`setup.cmd` also installs user-level launchers in `%USERPROFILE%\.context-kernel\bin` and adds that directory to the user PATH. After opening a new terminal, `akernel` works from any directory, starts the interactive agent session by default, and uses that current directory as the workspace location. Environment lookup prefers the current project `.env`, then falls back to the installed Context Kernel project `.env`. `akernel-chat` remains as a compatibility shortcut.

### Manual Python Install

```powershell
python -m pip install -e .[dev]
akernel --help
```

Python 3.10 or newer is required.

## Quick Start

```powershell
akernel setup
akernel init . --scan
akernel
akernel --ui tui
akernel --provider mock
```

Inside the interactive session, type a task and press Enter. Bare `akernel` accepts chat flags such as `--provider mock`, `--model`, `--aux-model`, `--max-steps`, and `--ui tui` without requiring the explicit `chat` subcommand. `--ui auto` uses the full-screen terminal UI on real terminals and falls back to classic output for CI, pipes, and tests. `akernel init . --scan` or `akernel project scan` writes a compact `.akernel/project.json` profile with detected languages, package managers, key files, project instruction files such as `AGENTS.md`, and test/build commands; this profile enters future context packets without loading the whole repository. If you ask to `run tests` or `verify` without naming a command, the agent prefers the scanned project test command. If you ask it to fix failing tests, the agent can run that profile test command, inspect one or more failing files from the command output, apply a bounded patch or rollback-safe batch patch when the failure is simple enough, and rerun verification. Use `/status` for the live workspace view, `/model` for primary and auxiliary model roles, `/cost` for the last run's token report, `/task` to inspect the current task session, and `/exit` to leave. The shell UI renders a cockpit-style dashboard with a status header, command strip, transcript, mission panel, model stack, workspace summary, task progress, run timeline, diagnostics, and assistant responses. In `--model-routing auto` mode, low/medium first-step planning can run on the auxiliary model while high-risk, deep, warning-heavy, or synthesis steps stay on the primary model. In `--aux-review auto` mode, auxiliary review runs before primary-model steps and is included in token cost reports.

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
```

`bench gate` requires the current benchmark checks to pass, then compares behavior and token cost against a saved baseline.

## OpenAI-Compatible Provider

Provider configuration is project-local. Copy `.env.example` to `.env` or use `setup.cmd -ForceEnv`.

```env
CONTEXT_KERNEL_OPENAI_API_KEY=replace-with-your-key
CONTEXT_KERNEL_OPENAI_BASE_URL=https://clarmy.cloud/v1
CONTEXT_KERNEL_OPENAI_MODEL=gpt-5.5
CONTEXT_KERNEL_OPENAI_AUX_MODEL=gpt-5.3-codex
```

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
akernel compare "Summarize the project goal"
akernel eval run examples\evals\phase2.json
akernel eval cost <report-id>
akernel eval diff <before-id> <after-id> --fail-on-regression
akernel bench cost <report-id>
akernel bench diff <before-id> <after-id> --fail-on-regression
akernel agent run "Patch notes/plan.txt and run tests" --provider openai --max-steps 4
akernel agent cost <agent-run-id>
```

Inside `akernel`, type a natural-language task and press Enter. Use `/cost` for the last run's token report, `/task` to inspect the current task session, and `/exit` to leave.

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
