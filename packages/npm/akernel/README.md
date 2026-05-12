# @context-kernel/akernel

This package is a small npm launcher wrapper for the Python `context-kernel` CLI.

```bash
npm install -g @context-kernel/akernel
akernel setup
akernel
```

The launcher forwards arguments to `python -m context_kernel.cli`. If the Python package is missing, it attempts a user-level bootstrap with:

```bash
python -m pip install --user --upgrade context-kernel
```

Useful environment overrides:

- `AKERNEL_PIP_SOURCE=git+https://github.com/huanxin0825-ctrl/context-kernel.git` installs from GitHub instead of PyPI.
- `AKERNEL_SKIP_BOOTSTRAP=1` disables automatic pip installation.

Python 3.10 or newer is required. The Python package remains the source of truth.
