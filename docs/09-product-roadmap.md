# Product Roadmap

This roadmap defines what "done" means for the post-alpha product surface.

## Code Editing Intelligence

Current alpha:

- Policy-gated `read_file`, `patch_file`, `batch_patch`, `write_file`, and `run_command`.
- Project profile commands for test/build/lint selection.
- Simple failing-test repair for deterministic Python failures.
- Packaged `multi_file_bugfix` skill contract for lower-token routing.

Next bar:

- Multi-file repair should produce a short root-cause hypothesis before editing.
- Coupled edits should use one `batch_patch` action when possible.
- Verification should rerun the project profile command and classify changed failures.
- Reports should separate "fixed", "partially fixed", and "needs human review".

## Long-Term Task Planning

Current alpha:

- Task sessions store compact checkpoints and linked traces.
- `task brief` builds resume context without replaying full chat.
- Packaged `long_task_planning` skill contract captures milestone discipline.

Next bar:

- Add milestone objects with verification signals.
- Add stale checkpoint pruning after task completion.
- Add task-level progress summaries that can be pulled into future projects only by explicit user action.

## Context Compaction And Memory Eviction

Current alpha:

- Memory records are typed, deduped, searchable, and archivable.
- `memory prune` archives lower-priority active records by count or token budget.
- Packaged `context_compaction` skill contract defines safe compaction rules.

Next bar:

- Add trace-backed "recoverability" scoring before pruning.
- Add per-project memory retention policies in `.akernel/config.json`.
- Add benchmark fixtures that measure memory precision, not just token savings.

## TUI Experience

Current alpha:

- Bare `akernel` starts an interactive session.
- The shell view shows workspace, model roles, commands, task progress, run summaries, costs, and diagnostics.
- `--ui auto|classic|tui` switches between classic stream output and the full-screen ANSI terminal UI.
- The TUI keeps a transcript, fixed session sidebar, last-run summary, diagnostics, pending context count, and bottom input hint without adding runtime dependencies.

Next bar:

- Keep plain output mode for CI and screen readers.
- Make long command output collapsible while preserving trace links.
- Add keyboard shortcuts and collapsible sections after the zero-dependency TUI stabilizes.

## Plugin And Skill Marketplace

Current alpha:

- Packaged marketplace index ships with built-in skills.
- `skill market-list` and `skill market-install` install skills into the current workspace.

Next bar:

- Support signed remote marketplace indexes.
- Add semantic version and compatibility metadata.
- Add skill trust prompts before installing remote sources.

## Cross-Project Global Memory

Current alpha:

- `memory global-push` copies active project memories into a user-level global store.
- `memory global-pull` copies selected global memories into a project.
- Sync is explicit and deduped; global memory does not silently enter project context.

Next bar:

- Add review prompts before pulling global memories.
- Add namespace filters, source project filters, and retention policies.
- Add import/export bundles for team handoff.

## Packaging And Distribution

Current alpha:

- Python package metadata exposes the `akernel` console script.
- `scripts/install_remote.ps1` installs from GitHub and creates a user-level launcher.
- `scripts/release_check.ps1` runs local release validation.
- A thin npm launcher wrapper lives under `packages/npm/akernel`.

Next bar:

- Publish Python package to PyPI after release credentials are configured.
- Publish npm wrapper after the Python package is available.
- Add CI release workflow with trusted publishing instead of local tokens.

## Large-Scale Benchmarks

Current alpha:

- `bench run`, `bench gate`, `bench diff`, `bench cost`, and `bench export` exist.
- `examples/benchmarks/scale` adds broader fixtures for context pressure, editing, global memory, and marketplace flows.

Next bar:

- Add at least 50 representative tasks before claiming measured savings publicly.
- Include real-provider smoke reports separately from deterministic mock reports.
- Publish markdown benchmark artifacts with each GitHub release.
