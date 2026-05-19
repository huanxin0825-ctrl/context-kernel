# Release Dry Run

Date: 2026-05-18

This document records the latest local release rehearsal. It intentionally does not publish to PyPI, npm, or GitHub Releases. The goal is to verify that the artifacts, install paths, launcher smoke, and benchmark evidence are ready before creating a tag.

## Environment

| Item | Value |
| --- | --- |
| OS | Windows |
| Python | 3.12.10 |
| npm | 11.11.1 |
| Python package | `akernel-runtime` |
| npm package | `@context-akernel/akernel` |
| Package version checked | `0.1.25` |

## Commands Run

```powershell
git status --short
python --version
npm --version
powershell -ExecutionPolicy Bypass -File scripts\release_check.ps1
python -m venv <temp-venv>
<temp-venv>\Scripts\python.exe -m pip install --upgrade pip
<temp-venv>\Scripts\python.exe -m pip install dist\akernel_runtime-0.1.25-py3-none-any.whl
<temp-venv>\Scripts\python.exe scripts\install_smoke.py --command python-module
```

## Results

| Check | Result |
| --- | --- |
| Worktree clean before rehearsal | Passed |
| Unit tests via release check | Passed, `122` tests |
| Python build | Passed |
| `twine check dist/*` | Passed |
| CLI entrypoint help | Passed |
| Marketplace skill listing | Passed |
| Python module real file-task smoke | Passed |
| Installed `akernel` real file-task smoke | Passed |
| npm `pack --dry-run` | Passed |
| npm launcher real file-task smoke | Passed |
| Benchmark evidence generation | Passed |
| Fresh venv wheel install | Passed |
| Fresh venv real file-task smoke | Passed |

Generated Python artifacts:

```text
dist/akernel_runtime-0.1.25-py3-none-any.whl
dist/akernel_runtime-0.1.25.tar.gz
```

npm dry-run artifact name:

```text
context-akernel-akernel-0.1.25.tgz
```

Benchmark evidence summary from the dry run:

| Metric | Result |
| --- | ---: |
| Fixtures | `3` |
| Tasks | `6` |
| Checks | `12/12` |
| Average savings | `46.5%` |
| Kernel tokens | `1396` |
| Full-load baseline tokens | `2609` |

## Findings

- The local release path is healthy: build, metadata checks, real file-task smoke, npm launcher smoke, and benchmark evidence all passed.
- The wheel can be installed into a fresh virtual environment and can complete the same real file-task smoke outside the editable checkout.
- No publish credentials, trusted-publisher environments, GitHub Release creation, PyPI upload, or npm publish were exercised in this rehearsal.
- The existing `.github/release-notes/v0.1.25.md` covers the prior `0.1.25` loop-guard fix only. The current branch has substantial `Unreleased` changes, so publishing this branch should first bump the version and add matching release notes for the next tag.

## Post Dry-Run Hardening

Date: 2026-05-19

- Added `scripts/release_guard.py` to verify Python runtime, Python package, and npm package versions stay aligned.
- The guard also verifies matching release notes, changelog version headings, optional tag matching, and strict pre-publish `Unreleased` cleanup.
- The local release check runs the guard in non-strict mode, so development branches get a warning while publish/tag paths can fail closed.

## Release Gate

Current status: dry-run pass, not ready to tag unchanged.

Before a real public release:

- Move relevant `Unreleased` changelog entries into a new version section.
- Bump `pyproject.toml` and `packages/npm/akernel/package.json` together.
- Add `.github/release-notes/<new-tag>.md`.
- Run `python scripts/release_guard.py --strict-release --tag <new-tag>` and fix any metadata error before tagging.
- Confirm PyPI Trusted Publishing and npm publishing configuration are active.
- Run `powershell -ExecutionPolicy Bypass -File scripts\release_check.ps1` again after the version bump.
