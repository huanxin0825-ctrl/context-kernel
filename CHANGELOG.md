# Changelog

All notable changes to Context Kernel will be recorded in this file.

The project follows a pragmatic pre-1.0 changelog: breaking changes may occur, but they should be documented with migration notes when possible.

## Unreleased

### Added

- `akernel` now starts the interactive agent session by default, with `akernel-chat` kept as a compatibility shortcut.
- `akernel setup` configures project-local OpenAI-compatible provider settings, including primary and auxiliary model names.
- User-level launchers make `akernel` available from any directory after the Windows setup flow.
- Interactive chat now exposes richer session status, command palette, current task context, run summaries, cost inspection, paste mode, file attachments, and policy-checked command attachments.
- Primary and auxiliary model roles are tracked separately, with automatic routing and optional auxiliary review before primary-model steps.
- Project scanning writes `.akernel/project.json` with detected languages, package managers, key files, local instruction files, safe command roots, and likely test/build commands.
- Agent verification requests can prefer scanned project commands instead of guessing test, build, lint, or install commands.
- The agent loop can run a project test command, inspect simple Python failure output, apply a bounded patch, and rerun verification for simple failing-test repairs.
- The agent action parser can recover valid JSON actions wrapped in extra text or fenced code blocks, while still recording the strict contract miss.
- Agent runs now include compact failure diagnostics with category, reason, and next-step guidance for configuration, auth, network, endpoint, protocol, budget, policy, command, tool, malformed-action, and loop-guard failures.
- `memory prune` archives lower-priority active memories by record count or token budget, with dry-run support.
- `memory global-push` and `memory global-pull` provide explicit cross-project memory sync through a user-level global store.
- `skill market-list` and `skill market-install` install packaged skill contracts from the built-in marketplace.
- Skill marketplace indexes now support v2 metadata with skill versions, licenses, publisher, Context Kernel compatibility checks, file/HTTP(S) indexes, and explicit remote trust gates.
- Packaged marketplace skills now include multi-file bugfix, long task planning, and context compaction contracts.
- Release helper scripts and a thin npm launcher wrapper prepare the project for PyPI/npm distribution.
- Scale benchmark fixtures cover context pressure, editing, global memory, and marketplace workflows.
- Product roadmap documentation defines completion bars for TUI, advanced editing, memory retention, marketplace, distribution, and benchmark maturity.
- `akernel --ui auto|classic|tui` adds a zero-dependency full-screen terminal UI with transcript, session sidebar, last-run summary, diagnostics, pending context, and input hint.
- The TUI now uses a cockpit layout with a status header, command strip, task mission panel, model stack, workspace summary, and last-run action timeline.
- Failing-test recovery can now read multiple file candidates from command output and apply inferred multi-file fixes as one rollback-safe `batch_patch`.
- Long task sessions can now carry structured milestone plans, active checkpoints, acceptance criteria, and compact resume prompts through `task plan`, `task next`, and `task checkpoint`.
- `memory audit` and enhanced `memory prune` explain retention scores, token cost, pinned records, and trace-backed recoverability before archiving lower-value memories.

### Changed

- The current directory is now the default workspace for bare `akernel`, making the CLI behave more like a project-local coding agent.
- Agent reports are kept compact by default and point back to authoritative run and tool traces for full audit detail.
- Common one-tool model output shapes such as `{ "tool": ... }`, `{ "name": ..., "arguments": ... }`, and single OpenAI-style `tool_calls` are normalized into the canonical action contract.

## 0.1.0 - 2026-05-12

Initial alpha CLI release.

### Added

- CLI workspace initialization with `.akernel` local state.
- Structured memory storage with typed records.
- Skill registry with progressive contract loading.
- Context assembly, budget profiles, and token pressure reports.
- Mock and OpenAI-compatible providers.
- Policy checks for file and command operations.
- Policy-gated local tools for read, write, patch, batch patch, delete, and command execution.
- Resumable task sessions with compact task briefs.
- Bounded agent loop with tool planning, recovery reads, compact saved reports, and agent cost reports.
- Eval and benchmark runners with cost reports, diffs, and regression gates.
- `bench gate` for one-command benchmark validation.
- Windows setup and wake wrappers.
- CI workflow for tests, packaging, CLI smoke, benchmark gate, and Windows wake validation.
