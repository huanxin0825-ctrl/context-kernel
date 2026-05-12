# Local Wake Workflow

Context Kernel supports a local wake workflow for day-to-day development.

## First Install

```powershell
cd D:\Desktop\job\github\context-kernel
.\setup.cmd
akernel setup
```

To rewrite project-local provider configuration:

```powershell
.\setup.cmd -ApiKey "<your-key>" -BaseUrl "https://clarmy.cloud/v1" -Model "gpt-5.5" -ForceEnv
```

Secrets are written only to project `.env`, which is ignored by git.

`setup.cmd` also installs global user-level launchers:

```powershell
akernel
akernel --help
```

The launchers live in `%USERPROFILE%\.context-kernel\bin`, and that directory is added to the user PATH. Open a new terminal if the commands are not visible immediately. `akernel` starts the default `.sandbox` workspace in the current directory and initializes it if needed. Environment lookup prefers the current directory `.env`, then falls back to the installed Context Kernel project `.env`. `akernel-chat` is kept as a compatibility shortcut.

## Wake The Project

```powershell
.\wake.cmd
```

Useful options:

```powershell
.\wake.cmd -InitWorkspace
.\wake.cmd -ListModels
.\wake.cmd -RunSmoke
```

If your machine already allows local PowerShell scripts, you can still call `.\setup.ps1` and `.\wake.ps1` directly.

## Interactive Chat

After waking the project and initializing a workspace, start the Claude Code-style CLI loop with:

```powershell
akernel
akernel --workspace .sandbox chat
```

Type a task and press Enter. Use `/cost` to inspect the last run's token pressure, `/task` to print the current task session, and `/exit` to leave.

## Check Configuration

```powershell
akernel doctor
```

The doctor command reports whether project provider configuration exists without printing the API key.
