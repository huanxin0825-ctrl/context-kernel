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
.\wake.cmd
```

`setup.cmd` creates `.venv`, installs the local CLI in editable mode, and prepares project-local `.env` if needed. `wake.cmd` activates the environment, loads `.env`, and prints common commands. The `.cmd` wrappers avoid local PowerShell execution-policy friction.

### Manual Python Install

```powershell
python -m pip install -e .[dev]
akernel --help
```

Python 3.10 or newer is required.

## Quick Start

```powershell
akernel init .sandbox
akernel --workspace .sandbox skill register examples\skills\edit_file.json
akernel --workspace .sandbox skill register examples\skills\context_budget.json
akernel --workspace .sandbox memory add --kind preference --text "Prefer CLI-first context budget prototypes." --tags cli
akernel --workspace .sandbox plan "Plan a CLI context budget prototype"
akernel --workspace .sandbox run "Summarize the project goal" --provider mock
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
akernel context "Continue this task" --task <task-id> --resume
akernel compare "Summarize the project goal"
akernel eval run examples\evals\phase2.json
akernel eval cost <report-id>
akernel eval diff <before-id> <after-id> --fail-on-regression
akernel bench cost <report-id>
akernel bench diff <before-id> <after-id> --fail-on-regression
akernel agent run "Patch notes/plan.txt and run tests" --provider openai --max-steps 4
akernel agent cost <agent-run-id>
```

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
