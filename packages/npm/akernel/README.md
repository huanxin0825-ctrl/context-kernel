# @context-akernel/akernel

This package is a small npm launcher wrapper for the Python Context Kernel runtime.

`akernel` means **Agent Kernel**: the command starts the agent-facing runtime that assembles context, memory, skills, policy, and token budgets before model calls.

```bash
npm install -g @context-akernel/akernel
akernel setup
akernel
```

The launcher forwards arguments to `python -m context_kernel.cli`. If the Python package is missing or older than this npm launcher, it attempts a user-level bootstrap or upgrade with:

```bash
python -m pip install --user --upgrade "akernel-runtime>=<launcher-version>"
```

Useful environment overrides:

- `AKERNEL_PIP_SOURCE=git+https://github.com/huanxin0825-ctrl/context-akernel.git` installs from GitHub instead of PyPI.
- `AKERNEL_SKIP_BOOTSTRAP=1` disables automatic pip installation.

Python 3.10 or newer is required. The Python package remains the source of truth.
