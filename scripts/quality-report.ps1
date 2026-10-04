# Prints the read-only data quality report. DATABASE_URL is read from .env into this process only and is never printed.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\quality-report.ps1 [-Samples 5]
param([int]$Samples = 5)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
Get-Content (Join-Path $root '.env') -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*(DATABASE_URL)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim(), 'Process') }
}
$env:PYTHONIOENCODING = 'utf-8'
& $py (Join-Path $root 'src\quality_main.py') --samples $Samples
