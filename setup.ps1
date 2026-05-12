param(
    [string]$ApiKey = "",
    [string]$BaseUrl = "https://clarmy.cloud/v1",
    [string]$Model = "gpt-5.5",
    [string]$AuxModel = "gpt-5.3-codex",
    [switch]$ForceEnv,
    [switch]$NoGlobalLauncher,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"
$EnvPath = Join-Path $ProjectRoot ".env"
$LauncherDir = Join-Path $env:USERPROFILE ".akernel\bin"

function Install-GlobalLaunchers {
    param(
        [string]$LauncherDir,
        [string]$ProjectRoot,
        [string]$PythonPath
    )

    New-Item -ItemType Directory -Force -Path $LauncherDir | Out-Null

$akernel = @"
@echo off
setlocal
set "AKERNEL_PROJECT_ROOT=$ProjectRoot"
"$PythonPath" -m context_kernel %*
exit /b %ERRORLEVEL%
"@
    Set-Content -LiteralPath (Join-Path $LauncherDir "akernel.cmd") -Value $akernel -Encoding ASCII

$chat = @"
@echo off
setlocal
set "AKERNEL_PROJECT_ROOT=$ProjectRoot"
"$PythonPath" -m context_kernel chat %*
exit /b %ERRORLEVEL%
"@
    Set-Content -LiteralPath (Join-Path $LauncherDir "akernel-chat.cmd") -Value $chat -Encoding ASCII

    if (($env:Path -split ';') -notcontains $LauncherDir) {
        $env:Path = "$LauncherDir;$env:Path"
    }
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ';') -notcontains $LauncherDir) {
        $newUserPath = if ($userPath) { "$LauncherDir;$userPath" } else { $LauncherDir }
        [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
        Write-Host "Added launcher directory to user PATH: $LauncherDir"
    }

    Write-Host "Installed global launchers:"
    Write-Host "  $(Join-Path $LauncherDir 'akernel.cmd')"
    Write-Host "  $(Join-Path $LauncherDir 'akernel-chat.cmd')"
}

Set-Location $ProjectRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found on PATH. Install Python 3.10+ and retry."
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-Host "Creating virtual environment: $VenvPath"
    python -m venv $VenvPath
}

Write-Host "Installing Context Kernel CLI in editable mode..."
& $PythonPath -m pip install -e .

if ($ForceEnv -or -not (Test-Path -LiteralPath $EnvPath)) {
    if (-not $ApiKey) {
        if (Test-Path -LiteralPath $EnvPath) {
            Write-Host "Keeping existing .env because no ApiKey was provided."
        } else {
            Write-Host "Creating .env from .env.example. Fill in AKERNEL_OPENAI_API_KEY before using openai provider."
            Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination $EnvPath
        }
    } else {
        $lines = @(
            "AKERNEL_OPENAI_API_KEY=$ApiKey",
            "AKERNEL_OPENAI_BASE_URL=$BaseUrl",
            "AKERNEL_OPENAI_MODEL=$Model",
            "AKERNEL_OPENAI_AUX_MODEL=$AuxModel"
        )
        Set-Content -LiteralPath $EnvPath -Value $lines -Encoding UTF8
        Write-Host "Wrote project-local .env."
    }
} else {
    Write-Host "Project .env already exists. Use -ForceEnv to rewrite it."
}

if (-not $NoGlobalLauncher) {
    Install-GlobalLaunchers -LauncherDir $LauncherDir -ProjectRoot $ProjectRoot -PythonPath $PythonPath
}

if ($Verify) {
    Write-Host "Verifying CLI..."
    & $PythonPath -m context_kernel models --provider mock
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Wake the project with:"
Write-Host "  .\wake.cmd"
Write-Host "Global commands:"
Write-Host "  akernel"
Write-Host "  akernel setup"
Write-Host "  akernel --help"
Write-Host "  akernel-chat  (compatibility shortcut)"
Write-Host "Open a new terminal if these commands are not found in the current one."
Write-Host "If you prefer the raw PowerShell entrypoint:"
Write-Host "  .\wake.ps1"
