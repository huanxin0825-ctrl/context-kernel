# Release Dry Run

Date: 2026-05-19

This document records the latest local release rehearsal. It intentionally does not publish to PyPI, npm, or GitHub Releases. The goal is to verify that the artifacts, install paths, launcher smoke, and benchmark evidence are ready before creating a tag.

## Environment

| Item | Value |
| --- | --- |
| OS | Windows |
| Python | 3.12.10 |
| npm | 11.11.1 |
| Python package | `akernel-runtime` |
| npm package | `@context-akernel/akernel` |
| Package version checked | `0.1.26` |

## Commands Run

```powershell
git status --short
python --version
npm --version
powershell -ExecutionPolicy Bypass -File scripts\release_check.ps1 -StrictReleaseMetadata
python -m venv <temp-venv>
<temp-venv>\Scripts\python.exe -m pip install --upgrade pip
<temp-venv>\Scripts\python.exe -m pip install dist\akernel_runtime-0.1.26-py3-none-any.whl
<temp-venv>\Scripts\python.exe scripts\install_smoke.py --command python-module
```

## Results

| Check | Result |
| --- | --- |
| Worktree clean before rehearsal | Passed |
| Unit tests via release check | Passed, `131` tests |
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
dist/akernel_runtime-0.1.26-py3-none-any.whl
dist/akernel_runtime-0.1.26.tar.gz
```

npm dry-run artifact name:

```text
context-akernel-akernel-0.1.26.tgz
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
- The source distribution includes Python release helper scripts for metadata guard and install smoke validation.
- The release workflow now checks source distribution helper scripts and runs the npm launcher smoke before publish jobs can proceed.
- No publish credentials, trusted-publisher environments, GitHub Release creation, PyPI upload, or npm publish were exercised in this rehearsal.
- `.github/release-notes/v0.1.26.md` is present and the strict release metadata guard passed for `v0.1.26`.

## Release Gate

Current status: `0.1.26` local dry-run pass, not published.

Before a real public release:

- Confirm the final commit is tagged as `v0.1.26`.
- Confirm PyPI Trusted Publishing and npm publishing configuration are active.
- Run `python scripts/release_guard.py --strict-release --tag v0.1.26` and `powershell -ExecutionPolicy Bypass -File scripts\release_check.ps1 -StrictReleaseMetadata` one last time on the exact tag commit.
