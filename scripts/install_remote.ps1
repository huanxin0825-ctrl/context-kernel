param(
  [string]$Source = "git+https://github.com/huanxin0825-ctrl/context-akernel.git",
  [string]$LauncherDir = "$env:USERPROFILE\.akernel\bin"
)

$ErrorActionPreference = "Stop"

python -m pip install --upgrade $Source

New-Item -ItemType Directory -Force -Path $LauncherDir | Out-Null
$launcher = Join-Path $LauncherDir "akernel.cmd"
@"
@echo off
python -m context_kernel.cli %*
"@ | Set-Content -Path $launcher -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($userPath.Split(";") -contains $LauncherDir)) {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$LauncherDir", "User")
}

Write-Host "installed akernel launcher: $launcher"
Write-Host "open a new terminal, then run: akernel setup"
