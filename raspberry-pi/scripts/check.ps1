# The gate. Nothing merges unless this passes.
#
# Run from raspberry-pi/ with the venv present:
#     .\scripts\check.ps1
#
# Stages are ordered cheapest-first so the fastest feedback comes first, but
# every stage runs even if an earlier one fails, because seeing all the damage
# at once beats fixing one class of problem per round-trip. The exit code is
# non-zero if any stage failed.
#
# See .claude/skills/anheart-strict-python/SKILL.md

$ErrorActionPreference = "Continue"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
$basedpyright = Join-Path $root ".venv\Scripts\basedpyright.exe"

if (-not (Test-Path $python)) {
    Write-Host "No venv found at .venv - create it first, with hashes required:" -ForegroundColor Red
    Write-Host "    uv venv --python 3.12 .venv"
    Write-Host "    uv pip install --python .venv\Scripts\python.exe --require-hashes -r requirements-dev-lock.txt"
    Write-Host "    uv pip install --python .venv\Scripts\python.exe --require-hashes --no-deps -r requirements-dev-nodeps-lock.txt"
    exit 1
}

$failures = @()

function Invoke-Stage {
    param(
        [string]$Name,
        [scriptblock]$Body
    )
    Write-Host ""
    Write-Host "=== $Name " -NoNewline -ForegroundColor Cyan
    Write-Host ("=" * [Math]::Max(1, 60 - $Name.Length)) -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        $script:failures += $Name
        Write-Host "FAILED: $Name" -ForegroundColor Red
    }
}

Invoke-Stage "lint (ruff)" { & $python -m ruff check . }
Invoke-Stage "format (ruff)" { & $python -m ruff format --check . }
Invoke-Stage "types (basedpyright, strict, zero Any)" { & $basedpyright }
Invoke-Stage "types (mypy, strict)" { & $python -m mypy . }
Invoke-Stage "tests + 100% branch coverage" {
    & $python -m pytest --cov --cov-branch --cov-fail-under=100
}

Write-Host ""
if ($failures.Count -gt 0) {
    Write-Host ("GATE FAILED: " + ($failures -join ", ")) -ForegroundColor Red
    exit 1
}
Write-Host "GATE PASSED" -ForegroundColor Green
exit 0
