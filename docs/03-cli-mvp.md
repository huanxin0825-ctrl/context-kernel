# CLI MVP

## User Workflows

### Install And Wake

```powershell
.\setup.cmd
.\wake.cmd
akernel setup
akernel doctor
akernel init . --scan
akernel project scan
```

`setup.cmd` creates `.venv`, installs the editable CLI, keeps provider credentials in project `.env`, and installs a user-level launcher so `akernel` can run from any directory after opening a new terminal. `akernel setup` writes API key, base URL, and model configuration into project `.env`. `wake.cmd` activates `.venv`, loads `.env`, and prints common commands.

`akernel doctor` also prints the workspace command allowlist resolved from `.akernel/config.json`.

### Initialize Workspace

```powershell
akernel init .
akernel init . --scan
akernel project scan
akernel project show
```

Creates `.akernel` state directories. `--scan` and `project scan` also write `.akernel/project.json`, a compact profile containing detected languages, package managers, key files, safe command roots, likely test/build commands, and short project instruction files such as `AGENTS.md`. The profile is injected into context packets as `runtime.project` so the model can understand the repository without loading the whole tree.

### Register Skill

```powershell
akernel skill register examples\skills\edit_file.json
akernel skill compile examples\skills\markdown\context_budget.md --register
akernel skill validate examples\skills\context_budget.json
akernel skill inspect context_budget --budget 300
akernel skill list
akernel skill show edit_file --level l1
```

### Add Memory

```powershell
akernel memory add --kind preference --text "Prefer CLI-first implementations."
akernel memory list
akernel memory search "CLI implementation"
akernel memory show <memory-id>
akernel memory update <memory-id> --tags cli,mvp
akernel memory forget <memory-id>
```

### Run Task

```powershell
akernel plan "Plan a minimal CLI implementation"
akernel plan "Delete .env and run git reset --hard"
akernel plan "Continue the current task" --task <task-id> --resume
akernel context "Continue the current task" --task <task-id> --resume
akernel run "Plan a minimal CLI implementation" --provider mock --budget 1200
akernel run "Plan a minimal CLI implementation" --provider mock --remember
akernel run "Plan a minimal CLI implementation" --provider mock --task <task-id>
akernel run "Continue the current task" --provider mock --task <task-id> --resume
akernel run "Plan a minimal CLI implementation" --provider openai --model gpt-5.5
akernel run "Return a compact JSON status" --provider openai --expect-json
akernel agent run "Continue the current task" --provider mock --task <task-id>
akernel agent run "Read notes/plan.txt and tell me what it says." --provider mock --max-steps 2
akernel agent run "Write notes/new.txt with 'hello cli' and run command python -V" --provider mock --max-steps 3
akernel agent run "Patch notes/plan.txt replace 'soon' with 'now' and run command pytest" --provider openai --max-steps 4
akernel agent run "Patch notes/plan.txt replace 'soon' with 'now' and run command python -m unittest discover -s tests" --provider openai --max-steps 3
akernel agent run "Continue the current task" --provider openai --task <task-id> --max-steps 5
akernel
akernel --provider mock --max-steps 1
akernel chat
akernel chat --task <task-id>
akernel agent list
akernel agent show <agent-run-id>
```

`plan` performs the same routing, memory retrieval, skill selection, budget check, and request-level policy warning scan as `run`, but stops before provider execution. Use it to inspect token cost and risky operation hints before spending real model tokens.

The command assembles a context packet, runs preflight verification, sends it to the provider only if the packet passes policy checks, verifies the response, and writes a trace. Over-budget packets are blocked by default; use `--allow-over-budget` only for deliberate experiments.
For OpenAI-compatible providers, put credentials in project `.env` using `.env.example` as the template.

`--remember` writes an explicit `task_state` memory for the run. If a verified response contains lines such as `Decision: ...` or `Fact: ...`, those marked lines are written as typed memory too.

`--resume` injects `task brief` into the context packet. It requires `--task <task-id>` and is the preferred way to continue multi-step work from a checkpoint.

`agent run` is the first bounded loop entrypoint. It creates or resumes a task, generates a plan, calls the provider with task resume context, requires a one-action JSON reply, executes one policy-gated tool when needed, feeds the resulting tool summary back into the task brief, and saves an agent report under `.akernel/agent_runs/`.

`akernel` without a subcommand starts an interactive Claude Code-style session in the current directory. Each normal line is sent through the same bounded `agent run` loop while reusing one task session, so the runtime can keep compact progress state instead of replaying the full conversation. The shell UI renders a session dashboard, command palette, task progress section, run summary, action trace, memory write count, and assistant response block. The model panel separates the primary execution model from the auxiliary planning/review/compression model. In `--model-routing auto` mode, low/medium first-step planning can run on the auxiliary model while high-risk, deep, warning-heavy, or synthesis steps stay on the primary model. In `--aux-review auto` mode, auxiliary review runs before primary-model steps and is included in saved traces and token reports. Built-in commands are `/help`, `/status`, `/model`, `/config`, `/compact`, `/paste`, `/task`, `/runs`, `/cost`, `/clear`, and `/exit`; `@path` attaches a workspace file and `!command` runs a policy-checked command for the next task.

Current action set:

- `respond`
- `read_file`
- `write_file`
- `patch_file`
- `batch_patch`
- `run_command`

Memory and skill commands now include productization primitives:

- `memory prune --max-records N --dry-run` previews memory records that would be archived under a retention cap.
- `memory prune --max-tokens N` keeps higher-priority records within an estimated memory token budget.
- `memory global-push` copies active project memories into an explicit user-level global store.
- `memory global-pull --limit N` copies reviewed global memories into the current project.
- `skill market-list` lists packaged marketplace skills.
- `skill market-install <skill-id>` installs a packaged skill contract into the current workspace.

Current multi-step pattern:

- `write_file -> run_command -> respond`
- `patch_file -> run_command -> respond`
- `batch_patch -> run_command -> respond`

The loop writes one compact summary memory per agent run by default, rather than one memory record per internal planning step. This keeps task memory useful without bloating retrieval.
Saved `agent run` reports are also compact by default, and keep trace ids so we can inspect full provider/tool detail only when needed.
It also stops repeated identical actions inside the same run before they become loops.
`patch_file` now supports structured modes such as `replace_all` and `occurrence`, so repeated text edits can stay inside patch semantics instead of forcing a full-file overwrite.
`patch_file` now also supports anchor block replacement with `start_anchor` and `end_anchor`, which is a better fit for config sections, function bodies, and Markdown blocks.
`batch_patch` applies multiple structured patch specs as one rollback-safe transaction, which keeps multi-file work compact without turning it into a large whole-file rewrite.
When `patch_file` or `run_command` fails in a recoverable edit flow, the runtime automatically adds a recovery `read_file` trace so the next step can plan from current file contents instead of guessing.
If the recovery read shows repeated matches for the requested patch text, the next step can retry `patch_file` with `replace_all=true` before falling back to `write_file`.
Policy-blocked tool actions stop the loop immediately.
The context packet now includes `runtime.command_policy.allowed_roots`, so the model can avoid proposing blocked commands before wasting a tool step.

This is intentionally narrow. The first versions favor auditability over broad tool autonomy.

### Inspect Agent Token Cost

```powershell
akernel agent list
akernel agent cost <run-id>
akernel agent cost <run-id> --json
```

`agent cost` reads a saved agent run and reports:

- Total input, output, and combined tokens.
- Per-step token totals.
- Task brief token growth across steps.
- Planned context token growth across steps.
- Action hotspots so we can see which loop step is the most expensive.

### Inspect Traces

```powershell
akernel trace list
akernel trace show <trace-id>
akernel trace verify <trace-id>
akernel trace remember <trace-id> --dry-run
akernel trace remember <trace-id>
```

### Check Policy

```powershell
akernel policy file write src\context_kernel\example.py
akernel policy file delete src\context_kernel\example.py
akernel policy file delete src\context_kernel\example.py --allow-destructive
akernel policy command -- python -m unittest discover -s tests
akernel policy command -- git reset --hard
```

Policy commands only inspect planned operations; they do not execute file changes or shell commands.
For commands, the result is workspace-aware: `.akernel/config.json` can extend `command_policy.allowed_roots` per project.

### Execute Tools

```powershell
akernel tool write notes\result.txt --text "hello tool layer"
akernel tool read notes\result.txt
akernel tool patch notes\result.txt --old "tool layer" --new "policy tool layer"
akernel tool patch notes\result.txt --old "policy" --new "tracked" --task <task-id>
akernel tool patch notes\result.txt --old "soon" --new "now" --replace-all
akernel tool patch notes\result.txt --old "same" --new "other" --occurrence 2
akernel tool patch notes\block.md --start-anchor "<!-- START -->" --end-anchor "<!-- END -->" --new "fresh body"
akernel tool patch notes\block.md --start-anchor "[[BEGIN]]" --end-anchor "[[END]]" --new "[[BEGIN]]\nnew\n[[END]]" --include-anchors
akernel tool batch-patch --specs-file patch-spec.json
akernel tool delete notes\result.txt
akernel tool delete notes\result.txt --allow-destructive
akernel tool exec -- python -c "print(123)"
akernel tool exec -- git reset --hard
akernel tool list
akernel tool show <tool-trace-id>
```

Tool commands execute only after policy passes. Blocked tool attempts still write tool traces, so denied actions are auditable. Pass `--task <task-id>` to automatically attach the tool trace to an active task.

Example `patch-spec.json`:

```json
[
  {"path": "notes/a.txt", "old": "old text", "new": "new text"},
  {"path": "notes/b.txt", "start_anchor": "<!-- START -->", "end_anchor": "<!-- END -->", "new": "fresh body"}
]
```

The batch accepts either a JSON array or an object with an `edits` array. If one edit fails, earlier edits from the same batch are restored and the failed batch still writes a trace.

Minimal command policy example:

```json
{
  "command_policy": {
    "allowed_roots": ["akernel", "git", "python", "pytest", "hostname"],
    "blocked_terms": []
  }
}
```

### Manage Tasks

```powershell
akernel task start "Build CLI task sessions" --goal "Make task progress resumable."
akernel task list
akernel task status <task-id>
akernel task brief <task-id>
akernel task brief <task-id> --json
akernel task step <task-id> --note "Implemented task store."
akernel task attach <task-id> tool <tool-trace-id>
akernel task attach <task-id> run <run-trace-id>
akernel task block <task-id> --note "Need user confirmation."
akernel task complete <task-id> --note "Finished task session MVP."
```

Task sessions hold compact, resumable progress checkpoints for multi-step work. Attach traces manually, or pass `--task <task-id>` to `run` and `tool` commands so traces are attached automatically. Use `task brief` to generate the minimal resume context for the next model call.

### Compare Against Baseline

```powershell
akernel compare "Plan a minimal CLI implementation" --budget 1200
akernel compare "Plan a minimal CLI implementation" --budget 1200 --json
```

The baseline loads all memory and all skills at `l3`. Context Kernel loads only selected records and progressive skill contracts.

### Run Eval Fixtures

```powershell
akernel eval run examples\evals\phase2.json
akernel eval run examples\evals\phase2.json --profile lean --json
akernel eval run examples\evals\phase2.json --execute --provider openai --model gpt-5.5
akernel eval list
akernel eval show <report-id>
akernel eval cost <report-id>
akernel eval diff <before-report-id> <after-report-id>
akernel eval diff <before-report-id> <after-report-id> --fail-on-regression
```

Eval fixtures reuse the same comparison engine and add expectation checks for selected skills, selected memory terms, response terms when `--execute` is used, and minimum savings. Executed eval tasks also run preflight verification; over-budget tasks are reported as blocked instead of spending provider tokens.
Reports are saved under `.akernel/evals/` by default. Use `--no-save` for temporary local experiments.
Use `eval cost` to see the hottest tasks, the weakest savings tasks, and execution-token concentration inside one saved eval report.
`eval diff` now also prints `cost_regressions`, `hotspot_delta`, and `weakest_savings_delta` so token regressions show up alongside correctness regressions.

### Run Benchmarks

```powershell
akernel bench run examples\benchmarks\phase2
akernel bench run examples\benchmarks\phase2 --execute --provider openai --model gpt-5.5
akernel bench gate examples\benchmarks\phase2
akernel bench gate examples\benchmarks\phase2 --baseline-report <report-id>
akernel bench list
akernel bench show <report-id>
akernel bench cost <report-id>
akernel bench diff <before-report-id> <after-report-id>
akernel bench diff <before-report-id> <after-report-id> --fail-on-regression
akernel bench export <report-id>
```

Benchmarks run every eval fixture in a directory and save aggregate reports under `.akernel/benchmarks/`.
`bench cost` summarizes the most expensive tasks across the whole benchmark, and `bench export` now includes that cost view in the generated Markdown.
`bench diff` now includes the same cost regression checks, which makes it much easier to spot when one fixture family starts dragging the whole benchmark backward.
`bench gate` is the one-command regression check: it runs a fresh benchmark, requires the current report checks to pass, compares it to either `--baseline-report` or the latest saved report for the same directory, and exits non-zero when regressions are found.

## MVP Command Surface

```text
akernel init [path]
akernel init [path] [--scan] [--no-config-update]
akernel project scan [--no-config-update] [--json]
akernel project show [--json]
akernel skill register <json-file>
akernel skill compile <markdown-file> [--id skill-id] [--output skill.json] [--register]
akernel skill validate <json-file>
akernel skill inspect <skill-id> [--budget n]
akernel skill list
akernel skill show <skill-id> [--level l0|l1|l2|l3]
akernel memory add --kind <kind> --text <text> [--tags tag1,tag2]
akernel memory show <memory-id> [--include-archived]
akernel memory list [--kind <kind>] [--all]
akernel memory search <query> [--kind <kind>] [--limit n]
akernel memory update <memory-id> [--kind <kind>] [--text <text>] [--tags tag1,tag2]
akernel memory forget <memory-id>
akernel doctor
akernel models [--provider mock|openai] [--base-url url]
akernel setup [--api-key key] [--base-url url] [--model model-id] [--aux-model model-id] [--env-file .env] [--force] [--verify]
akernel plan <request> [--budget n] [--profile lean|balanced|deep] [--task task-id] [--resume] [--json]
akernel agent run <request> [--provider mock|openai] [--model model-id] [--aux-model model-id] [--model-routing auto|primary|auxiliary] [--aux-review auto|off|always] [--base-url url] [--budget n] [--profile lean|balanced|deep] [--task task-id] [--max-steps n(default 5)] [--no-remember] [--allow-over-budget] [--expect-json] [--json]
akernel chat [--provider mock|openai] [--model model-id] [--aux-model model-id] [--model-routing auto|primary|auxiliary] [--aux-review auto|off|always] [--base-url url] [--budget n] [--profile lean|balanced|deep] [--task task-id] [--title title] [--max-steps n(default 5)] [--no-remember] [--allow-over-budget] [--expect-json]
akernel agent list
akernel agent show <agent-run-id>
akernel agent cost <agent-run-id> [--json]
akernel policy file <read|write|delete> <path> [--allow-destructive] [--json]
akernel policy command [--allow-destructive] [--json] -- <command...>
akernel tool read <path> [--max-chars n] [--task task-id] [--json]
akernel tool write <path> --text <text> [--task task-id] [--json]
akernel tool patch <path> [--old <text> | --start-anchor <text> --end-anchor <text>] --new <text> [--replace-all] [--occurrence n] [--include-anchors] [--task task-id] [--json]
akernel tool batch-patch --specs-file <json> [--task task-id] [--json]
akernel tool delete <path> [--allow-destructive] [--task task-id] [--json]
akernel tool exec [--allow-destructive] [--timeout n] [--task task-id] [--json] -- <command...>
akernel tool list
akernel tool show <tool-trace-id>
akernel task start <title> [--goal goal]
akernel task list [--status active|blocked|completed]
akernel task status <task-id> [--json]
akernel task brief <task-id> [--json]
akernel task step <task-id> --note <note>
akernel task attach <task-id> <run|tool|memory> <ref-id>
akernel task block <task-id> --note <note>
akernel task complete <task-id> [--note note]
akernel run <request> [--provider mock|openai] [--model model-id] [--base-url url] [--budget n] [--allow-over-budget] [--expect-json] [--remember] [--task task-id] [--resume]
akernel context <request> [--budget n] [--profile lean|balanced|deep] [--task task-id] [--resume]
akernel compare <request> [--budget n] [--json]
akernel eval run <fixture.json> [--budget n] [--profile lean|balanced|deep] [--execute] [--provider mock|openai] [--model model-id] [--json] [--no-save]
akernel eval list
akernel eval show <report-id>
akernel eval cost <report-id> [--json]
akernel eval diff <before-report-id> <after-report-id> [--json] [--fail-on-regression]
akernel bench run <fixture-directory> [--budget n] [--profile lean|balanced|deep] [--execute] [--provider mock|openai] [--model model-id] [--json] [--no-save]
akernel bench gate <fixture-directory> [--budget n] [--profile lean|balanced|deep] [--execute] [--provider mock|openai] [--model model-id] [--baseline-report report-id] [--require-baseline] [--json]
akernel bench list
akernel bench show <report-id>
akernel bench cost <report-id> [--json]
akernel bench diff <before-report-id> <after-report-id> [--json] [--fail-on-regression]
akernel bench export <report-id> [--output report.md]
akernel trace list
akernel trace show <trace-id>
akernel trace verify <trace-id> [--expect-json]
akernel trace remember <trace-id> [--dry-run]
```

## First Skill Schema

```json
{
  "id": "edit_file",
  "name": "Edit File",
  "summary": "Modify local files with scoped changes.",
  "intent": "Use when a task requires changing source or documentation files.",
  "inputs": ["file_path", "change_request"],
  "outputs": ["patch", "verification_result"],
  "constraints": ["Preserve unrelated changes.", "Prefer patch-based edits."],
  "failure_modes": ["Ambiguous target file.", "Conflicting existing changes."],
  "procedure": ["Inspect target file.", "Apply focused patch.", "Run relevant verification."],
  "examples": ["Update a CLI help string and run the command smoke test."]
}
```

## MVP Quality Bar

- Commands produce concise, useful output.
- Broken skill files fail with actionable errors.
- Context packets include token estimates.
- Trace files are readable JSON.
- The mock provider makes development deterministic.
