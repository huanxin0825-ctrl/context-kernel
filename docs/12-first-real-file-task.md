# First Real File Task

This walkthrough takes a new user from first launch to a traceable file edit. It avoids mock-only demos: by the end, `akernel` has created, appended, patched, read, and traced a real workspace file.

## 1. Install And Configure

From a cloned repository on Windows:

```powershell
.\setup.cmd
akernel setup
```

From PyPI on any platform:

```powershell
python -m pip install --user akernel-runtime
akernel setup
```

`akernel setup` writes project-local OpenAI-compatible settings. For an offline smoke path, use `--provider mock` when running chat or agent commands.

## 2. Create A Workspace

Run these commands inside the project or folder you want the agent to work on:

```powershell
akernel init . --scan
akernel doctor
```

`init --scan` creates `.akernel/` state and records a compact project profile. `doctor` verifies the local runtime, marketplace skills, and workspace state.

## 3. Run A Direct File Task

These commands exercise the same policy-gated file tools the agent uses:

```powershell
akernel tool create notes\first-task.txt --text "first"
akernel tool append notes\first-task.txt --text " second"
akernel tool patch notes\first-task.txt --old "second" --new "third"
akernel tool read notes\first-task.txt
```

Expected result:

```text
first third
```

Each mutation prints a transaction line such as:

```text
transaction: <id> committed snapshots=1
```

That means the runtime captured the file boundary for this operation and saved the result in a tool trace.

## 4. Run The Same Kind Of Task Through The Agent

For a deterministic local path:

```powershell
akernel --provider mock
```

Then ask:

```text
Create notes/agent-task.txt with hello agent, then run python -c "print('ok')"
```

For a real OpenAI-compatible provider, first make sure `.env` contains `AKERNEL_OPENAI_API_KEY`, `AKERNEL_OPENAI_BASE_URL`, and `AKERNEL_OPENAI_MODEL`, then run:

```powershell
akernel
```

The agent should show a compact status flow, execute file operations through policy-gated tools, and save task/run/tool traces under `.akernel/`.

## 5. Resume Or Inspect The Work

Use these when something is interrupted or you want evidence:

```powershell
akernel task list
akernel task brief <task-id>
akernel trace list
akernel trace show <trace-id>
```

`task brief` prints a continuation command so a long task can be resumed without replaying the whole conversation.

## 6. Local Release Smoke

Before publishing or handing a build to someone else, run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release_check.ps1
python scripts\install_smoke.py --command akernel
```

The install smoke creates a temporary workspace and verifies create, append, patch, and read through the installed `akernel` command.
