# Registers (or replaces) the per-user daily task "MoaHome-Daily". No password is stored:
# the task runs only while the user is logged on. Missed runs start when the PC is available again.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 [-At 09:10]
param([string]$At = '09:10', [string]$Name = 'MoaHome-Daily')
$script = Join-Path $PSScriptRoot 'run-daily.ps1'
$arg = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $script + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# Run only when the network is up (collection and the database need it); retry up to 3 times, 10 minutes apart, if the run fails.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew `
    -RunOnlyIfNetworkAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'MoaHome: daily announcement collection then receipt reminders (local trial)' | Out-Null
Get-ScheduledTask -TaskName $Name | Select-Object TaskName, State
