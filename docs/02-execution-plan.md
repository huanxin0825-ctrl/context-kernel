# Execution Plan

## Phase 1: CLI Runtime Skeleton

Goal: prove the context assembly loop works end to end.

Deliverables:

- Project package and CLI entry point.
- Workspace initialization.
- JSON skill registry.
- JSONL memory store.
- Token estimator.
- Context budgeter.
- Mock model provider.
- Trace writer.
- Smoke tests through CLI commands.

Exit criteria:

- A user can initialize a workspace, add memory, register a skill, run a task, and inspect the token report.

## Phase 2: Better Routing and Retrieval

Goal: improve relevance without increasing prompt size.

Deliverables:

- Memory scoring by kind, recency, and lexical match.
- Skill scoring with explicit routing reasons.
- Configurable token budget profiles.
- Context packet diff view.
- Baseline comparison command.

Exit criteria:

- The CLI can show why it selected each memory and skill.
- Benchmark tasks show lower context than a naive full-load baseline.

## Phase 3: Real Model Provider

Goal: connect the runtime to an actual LLM without coupling the core to one vendor.

Deliverables:

- Provider interface stabilization.
- OpenAI-compatible provider adapter.
- Environment-variable based configuration.
- Request/response redaction options.
- Provider failure handling.

Exit criteria:

- The same `run` command can use mock or real provider.
- Traces remain provider-neutral.

## Phase 4: Verifier and Policy Layer

Goal: move behavioral constraints out of the prompt.

Deliverables:

- Verifier plugin interface.
- Budget verifier.
- Dry-run execution planner.
- Output schema verifier.
- File operation policy hooks.
- Tool permission contracts.
- Policy-gated local tool executor.
- Resumable task session checkpoints.

Exit criteria:

- Common constraints can be enforced by code instead of natural-language reminders.
- Users can inspect route, budget, selected context, warnings, and expected savings before spending provider tokens.
- `run` blocks over-budget provider calls by default and requires an explicit override.
- Planned file and command operations can be checked by policy before execution.
- File reads, file writes, and safe shell commands can execute through policy-gated tool traces.
- Multi-step work can be resumed from task checkpoints without replaying full chat history.

## Phase 5: Evals and Public Proof

Goal: demonstrate measurable value.

Deliverables:

- Eval task suite.
- Baseline long-prompt runner.
- Context Kernel runner.
- Metrics: input tokens, output tokens, task success, retries, latency.
- Reproducible reports.

Exit criteria:

- Public benchmark report with clear savings and known failure modes.

## Engineering Rules

- Keep the core dependency-light until the runtime boundaries stabilize.
- Prefer boring storage formats early: JSON, JSONL, and plain text.
- Every optimization must have a trace or eval behind it.
- Avoid adding a UI before the CLI proves the architecture.
- Avoid turning the framework into a wrapper around one model vendor.
