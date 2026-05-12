# Contributing To Context Kernel

Thank you for helping make agent runtimes more inspectable, testable, and token-disciplined.

Context Kernel is early alpha software, so the best contributions are small, well-tested, and easy to reason about.

## Development Setup

```powershell
python -m pip install -e .[dev]
python -m unittest discover -s tests -p test_runtime.py
```

On Windows, the easiest local path is:

```powershell
.\setup.cmd
.\wake.cmd
```

## Contribution Principles

- Keep context spending visible. New routing, memory, skill, or agent behavior should expose enough trace data to explain token use.
- Prefer structured state over prompt text. Avoid adding long natural-language instructions when runtime data structures can carry the same meaning.
- Preserve policy boundaries. File edits, shell commands, and provider execution should stay behind explicit checks.
- Add benchmarks for optimizations. Token savings claims should be backed by eval or benchmark output.
- Keep dependencies boring. Add a dependency only when it clearly reduces complexity or enables a capability the standard library cannot reasonably provide.

## Pull Request Checklist

- Tests pass with `python -m unittest discover -s tests -p test_runtime.py`.
- CLI behavior is documented when command output or flags change.
- New token-sensitive behavior includes an eval, benchmark, trace, or cost-report check.
- No secrets, `.env`, local `.akernel` state, build artifacts, or virtualenv files are committed.
- User-facing errors are concise and actionable.

## Useful Local Checks

```powershell
python -m pip check
python -m unittest discover -s tests -p test_runtime.py
python -m build
akernel --workspace .sandbox bench gate examples\benchmarks\phase2 --require-baseline
```

## Issue Triage

When opening an issue, include:

- the command you ran
- the expected behavior
- the actual behavior
- relevant OS and Python version
- a minimal fixture, trace id, or benchmark report if available

Please redact API keys, provider responses containing secrets, and private project paths before sharing logs.
