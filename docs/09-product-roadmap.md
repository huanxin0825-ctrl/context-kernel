# Product Roadmap

This roadmap defines what "done" means for the post-alpha product surface.

## Code Editing Intelligence

Current alpha:

- Policy-gated `read_file`, `patch_file`, `batch_patch`, `write_file`, and `run_command`.
- Project profile commands for test/build/lint selection.
- Simple failing-test repair for deterministic Python failures, including multi-file candidate reads and inferred rollback-safe `batch_patch` edits for coupled trivial fixes.
- Packaged `multi_file_bugfix` skill contract for lower-token routing.

Next bar:

- Multi-file repair should produce a short root-cause hypothesis before editing.
- Coupled edits should use one `batch_patch` action when possible, with richer model-driven edit planning beyond deterministic mock inference.
- Verification should rerun the project profile command and classify changed failures.
- Reports should separate "fixed", "partially fixed", and "needs human review".

## Long-Term Task Planning

Current alpha:

- Task sessions store compact checkpoints and linked traces.
- `task brief` builds resume context without replaying full chat.
- `task start --plan`, `task plan`, `task next`, and `task checkpoint` manage structured milestone plans with active checkpoints, acceptance criteria, and resume prompts.
- Packaged `long_task_planning` skill contract captures milestone discipline.

Next bar:

- Add richer verification signals and failure classification to milestone objects.
- Add stale checkpoint pruning after task completion.
- Add task-level progress summaries that can be pulled into future projects only by explicit user action.

## Context Compaction And Memory Eviction

Current alpha:

- Memory records are typed, deduped, searchable, and archivable.
- `memory prune` archives lower-priority active records by count or token budget.
- `memory audit` and prune reports expose retention scores, token cost, pinned tags, and trace-backed recoverability.
- Packaged `context_compaction` skill contract defines safe compaction rules.

Next bar:

- Add per-project memory retention policies in `.akernel/config.json`.
- Add benchmark fixtures that measure memory precision, not just token savings.

## TUI Experience

Current alpha:

- Bare `akernel` starts an interactive session.
- The shell view shows workspace, model roles, commands, task progress, run summaries, costs, and diagnostics.
- `--ui auto|classic|tui` switches between classic stream output and the full-screen ANSI terminal UI.
- The TUI keeps a transcript, command strip, cockpit sidebar, task mission panel, model stack, last-run action timeline, diagnostics, pending context count, and bottom input hint without adding runtime dependencies.

Next bar:

- Keep plain output mode for CI and screen readers.
- Make long command output collapsible while preserving trace links.
- Add keyboard shortcuts and collapsible sections after the zero-dependency TUI stabilizes.

## Plugin And Skill Marketplace

Current alpha:

- Packaged marketplace index ships with built-in skills.
- `skill market-list` and `skill market-install` install skills into the current workspace.
- Marketplace v2 metadata includes skill version, license, publisher, Context Kernel compatibility, local/file/HTTP(S) index support, and explicit remote trust gates.

Next bar:

- Support signed remote marketplace indexes.
- Add detached signatures and checksum verification for remote sources.
- Add marketplace namespaces, update checks, and installed skill provenance reports.

## Cross-Project Global Memory

Current alpha:

- `memory global-push` copies active project memories into a user-level global store.
- `memory global-pull` copies selected global memories into a project.
- Dry-run previews, namespace filters, source-project filters, and tag filters make sync reviewable before copying.
- Provenance tags record namespace, source project, source root, and imported-global status for later audits.
- Sync is explicit and deduped; global memory does not silently enter project context.

Next bar:

- Add review prompts before pulling global memories.
- Add retention policies for imported global records.
- Add signed import/export bundles for team handoff.

## Packaging And Distribution

Current alpha:

- Python package metadata exposes the `akernel` console script.
- `scripts/install_remote.ps1` installs from GitHub and creates a user-level launcher.
- `scripts/release_check.ps1` runs local release validation, package metadata checks, and npm dry-run packing.
- An npm launcher wrapper lives under `packages/npm/akernel` and can bootstrap the Python package.
- `.github/workflows/release.yml` builds artifacts and supports trusted PyPI publishing plus guarded npm publishing.

Next bar:

- Publish Python package to PyPI after release credentials are configured.
- Publish npm wrapper after the Python package is available.
- Attach benchmark evidence artifacts to each GitHub release.

## Large-Scale Benchmarks

Current alpha:

- `bench run`, `bench gate`, `bench diff`, `bench cost`, `bench export`, and `bench evidence` exist.
- `examples/benchmarks/scale` adds broader fixtures for context pressure, editing, global memory, and marketplace flows.
- `bench evidence` generates a Markdown proof page with aggregate token savings, pass rate, strongest savings, and weakest savings across saved benchmark reports.

Next bar:

- Add at least 50 representative tasks before claiming measured savings publicly.
- Include real-provider smoke reports separately from deterministic mock reports.
- Publish benchmark evidence artifacts with each GitHub release.
