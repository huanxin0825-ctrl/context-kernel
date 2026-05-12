# Open Source Plan

Context Kernel should be presented as a serious runtime experiment, not as another agent wrapper.

The public story is:

> Agents should not spend tokens because the runtime cannot remember, route, or measure. Context Kernel makes context selection, memory, skill loading, tool use, and token cost explicit enough to test.

## Audience

Primary audience:

- developers building coding agents or research agents
- people maintaining large skill libraries
- teams worried about prompt bloat and hidden token costs
- researchers exploring agent memory and context selection

Secondary audience:

- local-first tool builders
- eval and benchmark authors
- users of OpenAI-compatible model gateways

## Positioning

Context Kernel is:

- a CLI-first runtime prototype
- a benchmarkable context assembly layer
- a structured memory and skill contract experiment
- a policy-gated tool execution environment

Context Kernel is not yet:

- a hosted agent platform
- a UI product
- a replacement for every existing agent framework
- a stable public API

## Launch Checklist

- README explains the problem, install path, quick start, and status.
- License is present and package metadata declares it.
- Contributing, security, conduct, and changelog files exist.
- CI runs tests, build, CLI smoke, benchmark gate, and Windows wake validation.
- `.env`, `.akernel`, virtualenv, build artifacts, and caches are ignored.
- First GitHub release should attach benchmark output and known limitations.

## Contribution Strategy

Accept small, focused pull requests first:

- new benchmark fixtures
- provider adapter improvements
- clearer traces and cost reports
- documentation from fresh setup attempts
- small routing and memory quality improvements

Defer broad rewrites until the CLI runtime has more real-world reports.

## Quality Bar

Every runtime change should answer at least one of these questions:

- Does it reduce unnecessary context without hiding required evidence?
- Does it make memory more durable, typed, or auditable?
- Does it make tool execution safer or easier to inspect?
- Does it improve benchmark success, cost, or reproducibility?
- Does it make the project easier for a new contributor to run?

## Public Roadmap

Near term:

- expand benchmark coverage
- add richer provider compatibility tests
- improve trace summarization and cost visualizations
- stabilize CLI report formats

Medium term:

- add optional embedding-backed retrieval
- support more durable memory backends
- expose a lightweight local service API
- design a UI only after the CLI proves the workflow
