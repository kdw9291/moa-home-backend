# MoaHome daily job: collect announcements, then send receipt-stage reminders.
# Registered by scripts/register-task.ps1. ASCII only (Windows PowerShell 5.1 safe).
# Secrets are read from .env into this process only, never printed; log output is redacted.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ('daily-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

$secretNames = @('DATA_GO_KR_API_KEY', 'SUPABASE_SECRET_KEY', 'DATABASE_URL', 'VAPID_PRIVATE_KEY')
$secretValues = @()
Get-Content (Join-Path $root '.env') -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)=(.*)$') {
        $v = $matches[2].Trim()
        [Environment]::SetEnvironmentVariable($matches[1], $v, 'Process')
        if ($secretNames -contains $matches[1] -and $v.Length -ge 8) { $secretValues += $v }
    }
}
$env:PYTHONIOENCODING = 'utf-8'

function Write-Log([string]$text) {
    foreach ($s in $secretValues) { $text = $text.Replace($s, '***') }
    if ($text.Length -gt 3000) { $text = $text.Substring(0, 3000) + '...(truncated)' }
    Add-Content -Path $log -Value $text -Encoding UTF8
}

function Invoke-Step([string]$name, [string]$script) {
    Write-Log ('[{0}] start {1}' -f (Get-Date -Format 's'), $name)
    $out = & $py (Join-Path $root ('src\' + $script)) 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-Log $out.Trim()
    Write-Log ('[{0}] end {1} exit={2}' -f (Get-Date -Format 's'), $name, $code)
    return $code
}

$collect = Invoke-Step 'collect' 'main.py'
# Reminders depend only on data already in the DB, so they run even if collection failed.
$notify = Invoke-Step 'notify' 'notify_main.py'

# Keep 30 days of logs.
Get-ChildItem $logDir -Filter 'daily-*.log' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force

if ($collect -ne 0 -or $notify -ne 0) { exit 1 }
exit 0
