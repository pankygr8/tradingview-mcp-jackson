# Registers the scanner agent as a Windows scheduled task.
#   - every 15 minutes, 08:40 -> 17:10, every day (the agent itself skips weekends/holidays)
#   - runs on battery, and catches up if the PC was asleep at a scheduled time
#   - never starts a second copy while one is still running
#
#   powershell -ExecutionPolicy Bypass -File install_agent_schedule.ps1          install / update
#   powershell -ExecutionPolicy Bypass -File install_agent_schedule.ps1 remove   delete
#   powershell -ExecutionPolicy Bypass -File install_agent_schedule.ps1 status   show last/next run
param([string]$Mode = "install")

$TaskName = "TradingScannerAgent"
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner   = Join-Path $Here "scanner_agent\scheduled_cycle.bat"

function Show-Status {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "Task '$TaskName' is not installed."; return }
    $i = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ""
    Write-Host "Task      : $TaskName  (state: $($t.State))"
    Write-Host "Command   : $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
    Write-Host "Last run  : $($i.LastRunTime)   result: $($i.LastTaskResult)  (0 = OK, 267011 = never ran)"
    Write-Host "Next run  : $($i.NextRunTime)"
    Write-Host "On battery: allowed = $(-not $t.Settings.DisallowStartIfOnBatteries)"
}

if ($Mode -eq "remove") {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed $TaskName"; exit 0
}
if ($Mode -eq "status") { Show-Status; exit 0 }

if (-not (Test-Path $Runner)) { Write-Error "Missing $Runner"; exit 1 }

# replace the old schtasks-created task (it had the battery restriction)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Runner`"" -WorkingDirectory $Here
$trigger = New-ScheduledTaskTrigger -Daily -At "08:40"
$rep     = New-ScheduledTaskTrigger -Once -At "08:40" `
             -RepetitionInterval (New-TimeSpan -Minutes 15) `
             -RepetitionDuration (New-TimeSpan -Hours 8 -Minutes 30)
$trigger.Repetition = $rep.Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Runs dashboard scanners, validates picks, rebuilds the leaderboard" | Out-Null

Write-Host "Installed $TaskName - every 15 min from 08:40 to 17:10, runs on battery, catches up after sleep."
Write-Host "Log    : $Here\scanner_agent\agent.log"
Write-Host "Output : $Here\scanner_agent\scheduler.out"
Write-Host "Report : $Here\scanner_agent\reports\leaderboard.html"
Show-Status
