# @context-kernel/akernel

This package is a small npm launcher wrapper for the Python `context-kernel` CLI.

It expects Python 3.10+ and the `context-kernel` Python package to be installed.

```bash
npm install -g @context-kernel/akernel
python -m pip install context-kernel
akernel setup
akernel
```

The wrapper intentionally stays thin so the Python package remains the source of truth.
