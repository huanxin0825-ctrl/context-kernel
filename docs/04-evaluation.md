# Evaluation Strategy

## Why Evals Matter

Token savings only matter if task quality stays steady or improves. Context Kernel needs evals from the beginning so the project does not become a collection of plausible ideas without proof.

## Metrics

- Input token estimate.
- Output token estimate.
- Total estimated tokens.
- Per-step agent-loop token totals.
- Task-brief token growth across agent steps.
- Planned context token growth across agent steps.
- Selected skill count.
- Selected memory count.
- Omitted context count.
- Task success.
- Retry count.
- Runtime errors.

## Baselines

### Full Prompt Baseline

Loads all runtime instructions, all matching skill documents, and a compressed history summary.

### Context Kernel Run

Loads only selected memory and progressive skill contracts under a budget.

## Current CLI Proof Command

The MVP includes a local comparison command:

```powershell
akernel compare "Plan a CLI context budget prototype" --budget 900
```

It reports:

- Estimated Context Kernel packet tokens.
- Estimated full-load baseline tokens.
- Savings in tokens and percent.
- Selected memory and skill counts.
- Baseline memory and skill counts.

Use `--json` when eval scripts need machine-readable packets.

For saved multi-step agent runs, inspect token pressure directly:

```powershell
akernel agent list
akernel agent cost <run-id>
```

## Fixture Runner

The MVP can run a JSON fixture:

```powershell
akernel eval run examples\evals\phase2.json
akernel eval run examples\evals\phase2.json --execute --provider openai --model gpt-5.5
akernel eval list
akernel eval show <report-id>
akernel eval cost <report-id>
akernel eval diff <before-report-id> <after-report-id>
akernel eval diff <before-report-id> <after-report-id> --fail-on-regression
```

Each task can declare:

- `expected_skills`: skill ids that should be selected.
- `expected_memory_terms`: terms that should appear in selected memory.
- `expected_response_terms`: terms that should appear in provider output when `--execute` is used.
- `minimum_savings_percent`: a minimum savings threshold against the full-load baseline.
- `profile`: optional budget profile for that task.

The summary reports total token estimates, average savings, passed checks, executed task count, and provider execution tokens when execution is enabled.
Use `eval cost` when you want the same report reframed around hotspots and weakest savings tasks instead of only aggregate totals.

Eval reports are stored in `.akernel/evals/` as JSON. This makes routing and budget changes auditable across time: run the same fixture after each change and compare the saved reports.

`eval diff` reports summary deltas and per-task deltas. Treat meaningful increases in kernel tokens, drops in savings percent, and lower passed-check counts as regressions to inspect. Tiny kernel-token changes under the MVP tolerance are reported but not counted as regressions.
It now also includes cost-focused regression signals such as hotspot token growth and weakest-savings drops.

## Benchmark Runner

The benchmark runner executes every eval fixture in a directory:

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
akernel bench evidence --limit 3 --fail-under 30 --output benchmark-evidence.md
```

Benchmark reports aggregate fixture count, task count, savings, checks, and provider execution tokens. They are stored in `.akernel/benchmarks/`.
Use `bench cost` to surface the highest-cost and lowest-savings tasks across the full benchmark.
Use `bench export` to produce a Markdown report for sharing or archiving outside the raw JSON store; it now includes the benchmark cost view alongside the normal summary tables.
Use `bench evidence` to summarize one or more saved benchmark reports into a release-ready proof page with report count, fixture count, task count, total kernel tokens, full-load baseline tokens, savings tokens, savings percent, pass rate, strongest savings, and weakest savings. Add `--fail-under N` when CI should reject evidence below a minimum total savings percentage.
The current deterministic scale snapshot is recorded in [Benchmark Evidence](10-benchmark-evidence.md).
`bench diff` includes the same cost regression checks so aggregate token backsliding is easier to catch before it becomes normal.
Use `bench gate` when you want that comparison as a single regression command: it runs the directory, requires the current report checks to pass, finds the latest matching saved baseline by path, and fails immediately if behavior or token cost regresses. Add `--require-baseline` when CI should fail instead of silently seeding a first run.

## First Eval Tasks

- Simple memory recall.
- Skill selection without full skill loading.
- Small documentation edit plan.
- Multi-step CLI task plan.
- Conflicting memory handling.
- Budget pressure with graceful omission.

## Report Format

Each eval should produce a JSON report and a human-readable summary:

```json
{
  "task_id": "memory_recall_001",
  "baseline_tokens": 1800,
  "kernel_tokens": 620,
  "savings_ratio": 0.655,
  "baseline_success": true,
  "kernel_success": true,
  "notes": "Kernel loaded one preference and one l1 skill contract."
}
```

## Guardrail

The project should not optimize for tiny prompts at the cost of silent incompetence. A smaller context packet is only good when it contains the right evidence.
