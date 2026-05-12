# Local Wake Workflow

Context Kernel supports a local wake workflow for day-to-day development.

## First Install

```powershell
cd D:\Desktop\job\github\context-kernel
.\setup.cmd
```

To rewrite project-local provider configuration:

```powershell
.\setup.cmd -ApiKey "<your-key>" -BaseUrl "https://clarmy.cloud/v1" -Model "gpt-5.5" -ForceEnv
```

Secrets are written only to project `.env`, which is ignored by git.

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

## Check Configuration

```powershell
akernel doctor
```

The doctor command reports whether project provider configuration exists without printing the API key.
