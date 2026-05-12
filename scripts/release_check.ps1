param(
  [switch]$SkipBuild,
  [switch]$SkipNpm,
  [switch]$SkipBenchmarkEvidence
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
  $VenvPython = Join-Path $RepoRoot ".venv/bin/python"
}
$PythonCommand = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

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
Write-Host "Python:" (& $PythonCommand --version)

Invoke-Checked $PythonCommand @("-m", "unittest", "discover", "-s", "tests", "-p", "test_runtime.py")

if (-not $SkipBuild) {
  Invoke-Checked $PythonCommand @("-m", "build")
  Invoke-Checked $PythonCommand @("-m", "twine", "check", "dist/*")
}

Invoke-Checked $PythonCommand @("-m", "context_kernel.cli", "--help")
Invoke-Checked $PythonCommand @("-m", "context_kernel.cli", "skill", "market-list")

if (-not $SkipNpm) {
  $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
  if (-not $npm) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
  }
  if ($npm) {
    Push-Location "packages/npm/akernel"
    try {
      Invoke-Checked $npm.Source @("pack", "--dry-run")
    }
    finally {
      Pop-Location
    }
  }
  else {
    Write-Host "npm not found; skipping npm package dry run"
  }
}

if (-not $SkipBenchmarkEvidence) {
  $benchRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("context-kernel-release-" + [System.IO.Path]::GetRandomFileName())
  try {
    Invoke-Checked $PythonCommand @("-m", "context_kernel.cli", "--workspace", $benchRoot, "init", $benchRoot)
    Invoke-Checked $PythonCommand @("-m", "context_kernel.cli", "--workspace", $benchRoot, "skill", "register", "examples/skills/edit_file.json")
    Invoke-Checked $PythonCommand @("-m", "context_kernel.cli", "--workspace", $benchRoot, "skill", "register", "examples/skills/context_budget.json")
    Invoke-Checked $PythonCommand @("-m", "context_kernel.cli", "--workspace", $benchRoot, "memory", "add", "--kind", "preference", "--text", "Prefer CLI-first context budget prototypes.", "--tags", "cli")
    Invoke-Checked $PythonCommand @("-m", "context_kernel.cli", "--workspace", $benchRoot, "bench", "run", "examples/benchmarks/scale")
    Invoke-Checked $PythonCommand @("-m", "context_kernel.cli", "--workspace", $benchRoot, "bench", "evidence", "--limit", "1", "--fail-under", "30", "--output", (Join-Path $benchRoot "benchmark-evidence.md"))
  }
  finally {
    if (Test-Path -LiteralPath $benchRoot) {
      Remove-Item -LiteralPath $benchRoot -Recurse -Force
    }
  }
}

Write-Host "release_check: ok"
