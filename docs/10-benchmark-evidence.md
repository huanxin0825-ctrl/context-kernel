# Benchmark Evidence

This page records the current deterministic benchmark evidence for Context Kernel's token discipline claim.

The numbers below are produced by the local mock-provider benchmark path, so they are reproducible without external API keys. Real-provider evidence should be reported separately because provider output tokens and model behavior vary.

## Reproduce

```powershell
akernel --workspace .sandbox-bench init .sandbox-bench
akernel --workspace .sandbox-bench skill register examples\skills\edit_file.json
akernel --workspace .sandbox-bench skill register examples\skills\context_budget.json
akernel --workspace .sandbox-bench memory add --kind preference --text "Prefer CLI-first context budget prototypes." --tags cli
akernel --workspace .sandbox-bench bench run examples\benchmarks\scale
akernel --workspace .sandbox-bench bench evidence --limit 1 --fail-under 30 --output .sandbox-bench\benchmark-evidence.md
```

## Current Snapshot

- Date: `2026-05-12`
- Benchmark: `examples\benchmarks\scale`
- Fixtures: `3`
- Tasks: `6`
- Checks: `12/12`
- Pass rate: `100.0%`
- Kernel tokens: `1235`
- Full-load baseline tokens: `2447`
- Token savings: `1212`
- Total savings: `49.53%`

## Fixture Coverage

| Fixture | Area | Tasks |
| --- | --- | ---: |
| `01-context-pressure.json` | context pressure and compaction | 2 |
| `02-agent-editing.json` | code editing and verification planning | 2 |
| `03-global-memory-marketplace.json` | global memory and skill marketplace selection | 2 |

## Strongest Savings

| Scope | Kernel | Baseline | Savings | Checks |
| --- | ---: | ---: | ---: | ---: |
| `scale/03-global-memory-marketplace.json/marketplace_skill_selection` | 152 | 410 | 258 (`62.93%`) | 2/2 |
| `scale/03-global-memory-marketplace.json/global_memory_reuse` | 169 | 404 | 235 (`58.17%`) | 1/1 |
| `scale/01-context-pressure.json/compact_long_task` | 182 | 408 | 226 (`55.39%`) | 2/2 |
| `scale/02-agent-editing.json/multi_file_bugfix_plan` | 216 | 411 | 195 (`47.45%`) | 2/2 |
| `scale/02-agent-editing.json/verification_command_profile` | 214 | 406 | 192 (`47.29%`) | 2/2 |

## Weakest Savings

| Scope | Kernel | Baseline | Savings | Checks |
| --- | ---: | ---: | ---: | ---: |
| `scale/01-context-pressure.json/budget_noisy_memory` | 302 | 408 | 106 (`25.98%`) | 3/3 |
| `scale/02-agent-editing.json/verification_command_profile` | 214 | 406 | 192 (`47.29%`) | 2/2 |
| `scale/02-agent-editing.json/multi_file_bugfix_plan` | 216 | 411 | 195 (`47.45%`) | 2/2 |
| `scale/01-context-pressure.json/compact_long_task` | 182 | 408 | 226 (`55.39%`) | 2/2 |
| `scale/03-global-memory-marketplace.json/global_memory_reuse` | 169 | 404 | 235 (`58.17%`) | 1/1 |

## Interpretation

This benchmark does not claim production-wide savings yet. It shows that the runtime can produce repeatable evidence that a smaller context packet preserves the expected task evidence while reducing estimated prompt tokens against a full-load baseline.

The next credibility bar is at least 50 representative tasks plus real-provider smoke reports published as release artifacts.
