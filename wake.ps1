param(
    [string]$Workspace = ".sandbox",
    [switch]$InitWorkspace,
    [switch]$ListModels,
    [switch]$RunSmoke
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvActivate = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"
$EnvPath = Join-Path $ProjectRoot ".env"

Set-Location $ProjectRoot

if (-not (Test-Path -LiteralPath $VenvActivate)) {
    throw "Virtual environment not found. Run .\setup.ps1 first."
}

. $VenvActivate

function Import-ProjectEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Host "No .env found. OpenAI provider will require project-local credentials."
        return
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        $name = $parts[0].Trim().Trim([char]0xFEFF)
        $value = $parts[1].Trim().Trim('"').Trim("'")
        Set-Item -Path "Env:$name" -Value $value
    }
}

Import-ProjectEnv $EnvPath

Write-Host "Context Kernel is awake."
Write-Host "Project: $ProjectRoot"
Write-Host "Workspace: $Workspace"
Write-Host "Model: $env:CONTEXT_KERNEL_OPENAI_MODEL"
Write-Host ""

if ($InitWorkspace) {
    akernel init $Workspace
}

if ($ListModels) {
    akernel models --provider openai
}

if ($RunSmoke) {
    akernel --workspace $Workspace run "In one short sentence, say what Context Kernel is testing." --provider openai --model $env:CONTEXT_KERNEL_OPENAI_MODEL --profile lean
}

Write-Host "Useful commands:"
Write-Host "  akernel models --provider openai"
Write-Host "  akernel init $Workspace"
Write-Host "  akernel --workspace $Workspace chat"
Write-Host "  akernel --workspace $Workspace bench run examples\benchmarks\phase2"
Write-Host "  akernel --workspace $Workspace bench gate examples\benchmarks\phase2"
Write-Host "  akernel --workspace $Workspace bench run examples\benchmarks\phase2 --execute --provider openai --model $env:CONTEXT_KERNEL_OPENAI_MODEL"
