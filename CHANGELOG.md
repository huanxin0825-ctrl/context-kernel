# Changelog

All notable changes to Context Kernel will be recorded in this file.

The project follows a pragmatic pre-1.0 changelog: breaking changes may occur, but they should be documented with migration notes when possible.

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
