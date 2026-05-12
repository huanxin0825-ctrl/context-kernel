# Release And CI

Context Kernel now has a repository-level CI path that checks the same things a contributor should trust locally:

- the package installs cleanly
- the runtime tests pass
- the CLI can run a benchmark and gate it against a saved baseline
- the Windows one-click wake flow still works

## GitHub Actions Workflow

The workflow lives at `.github/workflows/ci.yml`.

It runs:

- a cross-platform matrix on `ubuntu-latest` and `windows-latest`
- Python `3.10` and `3.12`
- `python -m unittest discover -s tests -p test_runtime.py`
- a CLI smoke path:
  - `init`
  - `skill register`
  - `memory add`
  - `bench run`
  - `bench gate --require-baseline`
- `python -m build` on the Ubuntu 3.12 lane
- a dedicated Windows job for `.\setup.cmd -Verify` and `.\wake.cmd -InitWorkspace`

## Local Mirror Of CI

You can mirror the important CI checks locally with:

```powershell
$env:PYTHONPATH="src"
python -m pip install -e .[dev]
python -m unittest discover -s tests -p test_runtime.py
python -m context_kernel init .sandbox-ci
python -m context_kernel --workspace .sandbox-ci skill register examples\skills\edit_file.json
python -m context_kernel --workspace .sandbox-ci skill register examples\skills\context_budget.json
python -m context_kernel --workspace .sandbox-ci memory add --kind preference --text "Prefer CLI-first context budget prototypes." --tags cli
python -m context_kernel --workspace .sandbox-ci bench run examples\benchmarks\phase2
python -m context_kernel --workspace .sandbox-ci bench gate examples\benchmarks\phase2 --require-baseline
python -m build
```

On Windows, also validate the wake flow:

```powershell
.\setup.cmd -Verify
.\wake.cmd -InitWorkspace -Workspace .sandbox-wake
```

## Release Shape

The current release shape is intentionally simple:

- editable local install: `pip install -e .`
- console entry point: `akernel`
- source distribution and wheel via `python -m build`
- project-local provider secrets via `.env`

This keeps the CLI portable while the runtime boundaries are still stabilizing.

`bench gate` also requires the current benchmark report itself to pass its checks. This prevents a bad first run from becoming the new normal just because the relative diff has no regression.

## Why This Matters

Context Kernel is trying to prove that token discipline can be part of the runtime instead of a prompt convention.
That claim is only credible when the benchmark, regression gate, and packaging path all stay reproducible outside one developer machine.
