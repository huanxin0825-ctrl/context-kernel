param(
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Command,
    [string[]]$Arguments = @()
  )
  & $Command @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "command failed ($LASTEXITCODE): $Command $($Arguments -join ' ')"
  }
}

Write-Host "== Context Kernel release check =="
$env:PYTHONPATH = "src"
Write-Host "Python:" (& python --version)

Invoke-Checked python @("-m", "unittest", "discover", "-s", "tests", "-p", "test_runtime.py")

if (-not $SkipBuild) {
  Invoke-Checked python @("-m", "build")
}

Invoke-Checked python @("-m", "context_kernel.cli", "--help")
Invoke-Checked python @("-m", "context_kernel.cli", "skill", "market-list")

Write-Host "release_check: ok"
