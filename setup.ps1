param(
    [string]$ApiKey = "",
    [string]$BaseUrl = "https://clarmy.cloud/v1",
    [string]$Model = "gpt-5.5",
    [switch]$ForceEnv,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"
$EnvPath = Join-Path $ProjectRoot ".env"

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
            Write-Host "Creating .env from .env.example. Fill in CONTEXT_KERNEL_OPENAI_API_KEY before using openai provider."
            Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination $EnvPath
        }
    } else {
        $lines = @(
            "CONTEXT_KERNEL_OPENAI_API_KEY=$ApiKey",
            "CONTEXT_KERNEL_OPENAI_BASE_URL=$BaseUrl",
            "CONTEXT_KERNEL_OPENAI_MODEL=$Model"
        )
        Set-Content -LiteralPath $EnvPath -Value $lines -Encoding UTF8
        Write-Host "Wrote project-local .env."
    }
} else {
    Write-Host "Project .env already exists. Use -ForceEnv to rewrite it."
}

if ($Verify) {
    Write-Host "Verifying CLI..."
    & $PythonPath -m context_kernel models --provider mock
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Wake the project with:"
Write-Host "  .\wake.cmd"
Write-Host "If you prefer the raw PowerShell entrypoint:"
Write-Host "  .\wake.ps1"
