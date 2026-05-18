from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test an installed akernel command.")
    parser.add_argument("--command", default="akernel", help="Console command to exercise.")
    args = parser.parse_args()

    command = resolve_command(args.command)
    with tempfile.TemporaryDirectory(prefix="akernel-install-smoke-") as tmp:
        workspace = Path(tmp)
        run([command, "init", str(workspace)])
        run([command, "--workspace", str(workspace), "tool", "create", "notes/smoke.txt", "--text", "first"])
        run([command, "--workspace", str(workspace), "tool", "append", "notes/smoke.txt", "--text", " second"])
        run([command, "--workspace", str(workspace), "tool", "patch", "notes/smoke.txt", "--old", "second", "--new", "third"])
        content = (workspace / "notes" / "smoke.txt").read_text(encoding="utf-8")
        if content != "first third":
            raise SystemExit(f"unexpected smoke file content: {content!r}")
        run([command, "--workspace", str(workspace), "tool", "read", "notes/smoke.txt", "--max-chars", "80"])
    print("install_smoke: ok")
    return 0


def resolve_command(command: str) -> str:
    if command == "python-module":
        return sys.executable
    resolved = shutil.which(command)
    if not resolved:
        raise SystemExit(f"command not found on PATH: {command}")
    return resolved


def run(command: list[str]) -> None:
    if command[0] == sys.executable:
        command = [sys.executable, "-m", "context_kernel.cli", *command[1:]]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(
            "command failed "
            f"({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
