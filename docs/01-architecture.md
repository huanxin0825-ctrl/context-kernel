# Architecture

## Runtime Pipeline

```text
User Request
  -> Intent Router
  -> Context Budgeter
  -> Memory Retriever
  -> Skill Registry
  -> Context Packet Builder
  -> Model Provider
  -> Verifier
  -> State Writer
  -> Trace Store
```

## Components

### Intent Router

Maps a request to likely capabilities and context needs. The first version uses simple keyword scoring so that behavior is transparent. Later versions can use embeddings or a small model.

### Context Budgeter

Allocates a fixed token budget across system instructions, task text, memory, skills, and tool contracts.

The budgeter should prefer:

- Task text first.
- Critical runtime constraints second.
- Skill contracts before full procedures.
- Recent and relevant memory over full history.
- Traceable omissions when budget is tight.

### Skill Registry

Stores skills as structured JSON contracts. Each skill exposes progressive load levels:

- `l0`: id, name, one-line summary.
- `l1`: inputs, outputs, and capability contract.
- `l2`: critical constraints and failure modes.
- `l3`: full procedure and examples.

The runtime starts small and escalates only when the budget and task complexity justify it.

### Memory Store

Stores typed records instead of compressed transcript blobs.

Initial memory kinds:

- `fact`: stable project or world facts.
- `preference`: durable user preferences.
- `project_state`: current repository or project state.
- `task_state`: active task progress.
- `decision`: past choices and rationale.

Memory v2 uses local SQLite as the primary state layer, with one-time migration from the earlier `memory.jsonl` append log. Records are de-duplicated by normalized kind and text, can be updated in place, and can be archived so forgotten records stop entering context without destroying auditability.

Memory retrieval uses a relevance gate before records enter the context packet. A record must match at least two effective terms, or match one strong domain term such as `context`, `budget`, `skill`, `runtime`, `memory`, `eval`, or `token`. This keeps weak one-word overlaps from pulling unrelated memory into small packets.

### Context Packet

A context packet is the exact payload given to the model provider. It is explicit, inspectable, and token-estimated before execution.

It contains:

- Request.
- Runtime instruction summary.
- Selected memories.
- Selected skill load levels.
- Budget report.
- Omissions and reasons.

### Model Provider

The model provider is an interface. The CLI includes a mock provider for deterministic local tests and an OpenAI-compatible provider for `/v1/models` and `/v1/chat/completions`.

Provider secrets are loaded from current process environment first, then project-local `.env`. The `.env` file is ignored by git and should stay local to the working copy.

- `CONTEXT_KERNEL_OPENAI_API_KEY`
- `CONTEXT_KERNEL_OPENAI_BASE_URL`
- `CONTEXT_KERNEL_OPENAI_MODEL`
- `CONTEXT_KERNEL_OPENAI_AUX_MODEL`

OpenAI-compatible base URLs are normalized to a `/v1` API root, so both `https://host` and `https://host/v1` are accepted.

`CONTEXT_KERNEL_OPENAI_MODEL` is the primary execution model used for high-risk, deep, warning-heavy, or synthesis steps. `CONTEXT_KERNEL_OPENAI_AUX_MODEL` is the auxiliary model role used for low/medium first-step planning in automatic routing and for auxiliary review before primary-model steps. Review traces are saved and their tokens are included in agent cost reports.

### Verifier

The verifier checks runtime invariants in code instead of relying on natural-language reminders.

Current checks:

- Preflight packet shape.
- Preflight budget guard, blocking provider execution by default when over budget.
- Non-empty provider response.
- Optional JSON response validation for structured-output tasks.

Later versions should validate file edits, tool calls, policy rules, and task-specific success criteria.

### Policy Contracts

Policy contracts describe whether a planned operation is allowed before any tool executes.

Current checks:

- File operations stay inside the workspace root.
- Sensitive files such as `.env` are blocked.
- Generated or internal directories such as `.venv` and protected `.akernel` state are blocked.
- Destructive file operations require an explicit override.
- Shell commands are checked against a small safe root list and destructive terms such as `git reset`, `remove-item`, `rm`, and `del`.
- The safe root list is now workspace-configurable through `.akernel/config.json`, so different repositories can opt into different command roots without forking the runtime.

The planner surfaces policy warnings from the user request so risky work can be reviewed before token-heavy model execution or tool use.

### Tool Executor

The tool executor is the first execution layer above policy contracts.

Current tools:

- `tool read <path>` reads a workspace file after file policy passes.
- `tool write <path> --text ...` writes a workspace file after file policy passes.
- `tool patch <path> --old ... --new ...` applies a structured replacement after file policy passes, including one-match, `replace_all`, nth-occurrence, and anchor-block modes.
- `tool batch-patch --specs-file <json>` applies multiple structured patch specs as one transaction. If any edit fails, previously applied edits are rolled back and the batch trace records the failure.
- `tool delete <path> --allow-destructive` deletes a file only after destructive policy is explicitly allowed.
- `tool exec -- <command...>` runs a safe command after command policy passes.
- Every allowed, failed, or blocked tool operation writes a trace under `.akernel/tool_traces/`.

This separates capability from permission: policy decides whether an operation is allowed, the tool executor performs it, and traces make the result auditable.

### Trace Store

Each run writes a trace containing:

- Request.
- Selected memories.
- Selected skills and load levels.
- Estimated token usage.
- Provider response.
- Preflight and response verifier results.
- Timestamps.

Traces are the proof layer. They make optimization measurable instead of vibes-based.

### State Writer

The state writer turns selected trace outcomes into structured memory only when explicitly requested.

Current behavior:

- `run --remember` writes a `task_state` memory for the completed run.
- `trace remember <trace-id>` can write memory from a saved trace after review.
- `trace remember <trace-id> --dry-run` shows candidates without writing.
- Response lines beginning with `Decision:`, `Fact:`, `Preference:`, `Project state:`, or `Task state:` are mapped to typed memory.
- Secret-looking values are redacted before memory write.

This keeps durable memory intentional instead of turning every model response into long-term state.

### Task Sessions

Task sessions are resumable checkpoints for multi-step work.

Current behavior:

- `task start` creates an active task with a title and goal.
- `task step` appends checkpoint notes.
- `task attach` links run traces, tool traces, or memory records to the task.
- `run --task <task-id>` and `tool ... --task <task-id>` attach traces automatically before work drifts away from the active checkpoint.
- `task brief` builds a compact resume context from recent steps and linked trace/memory summaries.
- `plan --task <task-id> --resume`, `context --task <task-id> --resume`, and `run --task <task-id> --resume` inject that brief into the context packet.
- `task block` marks a task as blocked with a reason.
- `task complete` closes the task and prevents further mutation.

Task sessions are not a replacement for memory. They are the active working state that lets the runtime resume from a compact checkpoint instead of replaying chat history.

### Agent Loop

Agent Loop v8 wraps the runtime pipeline in a bounded, auditable loop:

```text
task brief -> plan -> run provider -> verify -> write state -> attach trace -> checkpoint
```

Current behavior:

- `agent run` creates a task when one is not supplied, or resumes an existing active task.
- Each step injects the task brief into the context packet instead of replaying full history.
- Each provider run is asked to return exactly one JSON action.
- The current action set is `respond`, `read_file`, `write_file`, `patch_file`, `batch_patch`, and `run_command`.
- The primary create-and-verify path is now `write_file -> run_command -> respond`.
- The primary multi-step path is now `patch_file -> run_command -> respond`.
- The primary multi-file edit path is now `batch_patch -> run_command -> respond`.
- `patch_file` now supports structured replacement modes such as `replace_all` and nth-occurrence patching.
- `patch_file` also supports anchor block replacement with `start_anchor` and `end_anchor`, so bounded edits can target named regions instead of depending only on repeated text.
- `batch_patch` accepts the same text and anchor patch semantics as `patch_file`, but groups edits into a rollback-safe batch for multi-file work.
- If a patch or verification step fails in a recoverable edit flow, the runtime automatically performs a recovery `read_file` and attaches that trace before the next model step.
- When the recovery read shows repeated matches, the next step can retry with structured patch semantics before escalating to a whole-file rewrite.
- Tool actions are executed through the existing policy-gated tool executor.
- The context packet includes `runtime.command_policy.allowed_roots`, so the model can see the workspace command allowlist before deciding whether to request `run_command`.
- A saved `.akernel/project.json` profile can also enter the packet as `runtime.project`, giving the model compact project metadata such as language, package manager, key files, safe command roots, and likely test/build commands without loading the full repository.
- When a user asks to run tests, verify, build, lint, or install without naming an exact command, the agent should prefer the matching `runtime.project.commands` entry over guessing a command.
- Tool output summaries are attached back to the task brief for the next step.
- Each provider run writes a normal trace and attaches it to the task.
- One explicit task-state summary memory is written per agent run and attached back to the task.
- Agent reports saved under `.akernel/agent_runs/` are compact by default and point back to the authoritative run/tool traces for full audit detail.
- The agent action parser accepts the canonical `{ "action": ... }` contract plus common one-tool variants such as `{ "tool": ..., "args": ... }`, `{ "name": ..., "arguments": ... }`, and OpenAI-style single `tool_calls`. These shapes are normalized before policy execution, reducing wasted turns from harmless formatting drift.
- Repeated identical actions are stopped inside the same run to reduce loop risk.
- Policy-blocked tool actions stop the loop immediately instead of triggering further tool retries.
- Agent reports are saved under `.akernel/agent_runs/`.

The loop is intentionally conservative. It does not yet let model output trigger arbitrary tools automatically; local tool execution remains policy-gated through `akernel tool ...`.

Batch patch specs can be an array of edits or an object with an `edits` array:

```json
[
  {"path": "notes/a.txt", "old": "old text", "new": "new text"},
  {"path": "notes/b.txt", "start_anchor": "<!-- START -->", "end_anchor": "<!-- END -->", "new": "fresh body"}
]
```

## Data Layout

Runtime workspaces use a `.akernel` directory:

```text
.akernel/
  config.json
  project.json       # compact project scan profile
  memory.jsonl        # legacy import log
  memory.sqlite3      # primary Memory v2 state
  skills/
    *.json
  traces/
    *.json
  tool_traces/
    *.json
  agent_runs/
    *.json
  tasks/
    *.json
  evals/
    *.json
  benchmarks/
    *.json
```

## Design Boundary

The runtime should not hide expensive context decisions inside model calls. Every inclusion should be explainable before the model runs.
