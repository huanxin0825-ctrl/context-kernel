# Changelog

All notable changes to Context Kernel will be recorded in this file.

The project follows a pragmatic pre-1.0 changelog: breaking changes may occur, but they should be documented with migration notes when possible.

## Unreleased

No changes yet.

## 0.1.19 - 2026-05-14

### Added

- Added a live spinner while chat/agent work is running so provider calls no longer look frozen.
- Added one retry for transient OpenAI-compatible provider network failures.

### Changed

- Provider network diagnostics now include request URL, timeout, and the underlying network error.

## 0.1.18 - 2026-05-14

### Added

- Added chat-native skill operations: `/skills list`, `/skills show`, `/skills inspect`, `/skills recommend`, `/skills market-list`, and `/skills install`.
- Added automatic default-budget expansion for interactive/agent runs when no explicit `--budget` is provided.

### Fixed

- Fixed repeated chat turns getting blocked by the tiny default context budget after a few messages. Explicit `--budget` values remain strict.

## 0.1.17 - 2026-05-14

### Changed

- Changed default agent/chat model routing to `primary` so configured primary models handle work unless `--model-routing auto` is explicitly requested.
- Added live per-step progress events in chat and agent runs, including selected model role, model name, action, status, and token count.

## 0.1.16 - 2026-05-14

### Added

- Added interactive `/mcp refresh <name>` for refreshing MCP tool discovery inside chat.
- Added interactive `/mcp call <server> <tool> --args "{...}"` for manual MCP calls without leaving the session.
- Added interactive `/mcp enable <name>` and `/mcp disable <name>` toggles.

## 0.1.15 - 2026-05-14

### Added

- Added chat-first extension discovery commands: `/extensions`, `/mcp`, and `/skills`.
- Added extension availability to TUI session status and command completions.

### Changed

- Reordered TUI last-run details so recent actions stay visible even when the sidebar is crowded.

## 0.1.14 - 2026-05-14

### Added

- Added `mcp_call` to the agent action contract so the agent loop can call discovered MCP tools automatically.
- Added MCP call summaries, diagnostics, and saved action metadata for agent-driven MCP traces.

### Changed

- Updated the mock planning provider to exercise MCP calls in tests when enabled MCP tools are present.

## 0.1.13 - 2026-05-14

### Added

- Added `akernel mcp call <server> <tool>` for manual stdio MCP tool calls.
- MCP calls now write auditable `mcp_call` tool traces under `.akernel/tool_traces`.

## 0.1.12 - 2026-05-14

### Added

- Added `akernel mcp refresh <name>` to start a stdio MCP server, run `initialize` and `tools/list`, and save discovered tool summaries.
- Added a lightweight stdio JSON-RPC bridge for MCP tool discovery with timeout handling and process cleanup.

## 0.1.11 - 2026-05-14

### Added

- Added MCP v1 configuration commands for local stdio servers: `mcp add`, `mcp list`, `mcp show`, `mcp enable`, `mcp disable`, and `mcp remove`.
- Added `.akernel/mcp.json` workspace storage for enabled/disabled MCP servers and curated tool summaries.
- Added compact MCP summaries to context packets so server availability is visible without loading full tool schemas.

## 0.1.10 - 2026-05-14

### Changed

- Made default TUI assistant turns display only the assistant answer, moving run metadata into the compact status line.
- Switched prompt completions to a more visible readline-style menu and explicitly opens suggestions when `/` or `@` is typed.

## 0.1.9 - 2026-05-14

### Added

- Added inline `@path` attachment inside normal chat tasks, so exact workspace files can be referenced without a separate attach command.
- Added project and user Markdown slash commands under `.akernel/commands` and `~/.akernel/commands`, with `/commands` discovery and completion.

### Changed

- Reworked the default TUI startup into a quieter chat-first header while keeping the full cockpit dashboard available with `AKERNEL_ALT_SCREEN=1`.
- Improved command and file completion so `/` and `@` work on the current cursor token, including inline text such as `inspect @README`.

## 0.1.8 - 2026-05-14

### Changed

- Changed the default TUI loop to render the full workspace view once, then append only incremental user/status/assistant updates for each turn.

### Fixed

- Prevented normal chat turns from repeatedly printing the full fixed UI layout and filling the terminal scrollback.

## 0.1.7 - 2026-05-14

### Added

- Added real-time chat input completions for slash commands and `@` workspace file search.

### Changed

- Added `prompt_toolkit` as the interactive input dependency, with automatic fallback to native `input()` in non-interactive terminals.

## 0.1.6 - 2026-05-14

### Added

- Added TUI history viewport controls with `/up`, `/down`, and `/latest`.
- Added `@` file search for the current workspace, including numbered follow-up attachment commands such as `@1`.

### Changed

- Redesigned the interactive TUI into a cleaner, lower-density terminal workspace.
- Made the default TUI scrollback-friendly by avoiding alt-screen takeover unless `AKERNEL_ALT_SCREEN=1` is set.
- Improved CJK/wide-character alignment in TUI rows and truncation.

## 0.1.5 - 2026-05-12

### Fixed

- Fixed TUI chat sessions exiting on the first user message because the interactive state holder was not initialized.
- Made project-root `.env` fallback stable when a user-level parent directory also contains a `.env` file.

## 0.1.4 - 2026-05-12

### Changed

- Reissued the repository rename release after refreshing package publisher bindings for `context-akernel`.

## 0.1.3 - 2026-05-12

### Changed

- Renamed repository metadata and install links from `context-kernel` to `context-akernel` to align with the npm scope.

## 0.1.2 - 2026-05-12

### Changed

- Renamed the prepared npm launcher scope from `@context-kernel/akernel` to `@context-akernel/akernel`.
- Renamed project-local provider environment variables from `CONTEXT_KERNEL_OPENAI_*` to `AKERNEL_OPENAI_*` while keeping legacy names as a compatibility fallback.
- Moved the default user launcher directory from `%USERPROFILE%\.context-kernel\bin` to `%USERPROFILE%\.akernel\bin`.

## 0.1.1 - 2026-05-12

### Changed

- Documented the `akernel` naming convention as Agent Kernel across README, PyPI metadata, and npm launcher docs.
- Upgraded npm release guidance and workflow support for trusted publishing, provenance, and npm-only manual publishes.
- Added GitHub Release notes and release workflow support for automated or manual release page creation.
- Updated README installation wording to reflect the live PyPI package and the pending npm launcher publication.

## 0.1.0 - 2026-05-12

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
- Global memory sync supports dry-run previews, namespaces, source-project filters, tag filters, and provenance tags for controlled cross-project sharing.
- `skill market-list` and `skill market-install` install packaged skill contracts from the built-in marketplace.
- Skill marketplace indexes now support v2 metadata with skill versions, licenses, publisher, Context Kernel compatibility checks, file/HTTP(S) indexes, and explicit remote trust gates.
- Packaged marketplace skills now include multi-file bugfix, long task planning, and context compaction contracts.
- Release helper scripts and a thin npm launcher wrapper prepare the project for PyPI/npm distribution.
- Release workflow, PyPI metadata checks, npm package dry-run validation, and npm launcher bootstrap support prepare one-command remote installation.
- Release workflow now generates and uploads benchmark evidence artifacts alongside publish-ready build checks.
- Scale benchmark fixtures cover context pressure, editing, global memory, and marketplace workflows.
- `bench evidence` summarizes saved benchmark reports into JSON or Markdown proof pages with aggregate token savings, pass rate, strongest savings, weakest savings, and optional minimum-savings gates.
- Benchmark evidence documentation records the current deterministic scale snapshot and reproduction commands.
- Publishing setup documentation lists the PyPI Trusted Publishing, npm token, GitHub environment, and first-release steps needed for public release.
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

### Initial Alpha Baseline

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
