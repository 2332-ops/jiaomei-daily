<#
    Register Windows Scheduled Tasks for jiaomei_tracker.

    Creates one weekday task per slot defined in config.yaml. The slot list is
    read from the program itself (`run.py --print-schedule`), so config.yaml
    stays the single source of truth for send times.

    Usage (run in an elevated PowerShell is NOT required for user tasks):
        powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
        powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -PythonExe "C:\path\to\python.exe"

    Notes:
      - Tasks run only while the user is logged on (Interactive logon type),
        so keep the machine powered on and logged in.
      - StartWhenAvailable is enabled: if the machine was asleep/off at the
        trigger time, the task runs as soon as possible afterwards.
#>
param(
    [string]$PythonExe = "",
    [string]$TaskPrefix = "JiaomeiTracker"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$RunPy = Join-Path $ProjectDir "run.py"

if (-not (Test-Path $RunPy)) {
    throw "run.py not found at: $RunPy"
}

# ---- locate python interpreter -------------------------------------------------
if (-not $PythonExe) {
    $candidates = @(
        (Join-Path $ProjectDir ".venv\Scripts\python.exe"),
        (Join-Path $ProjectDir "venv\Scripts\python.exe"),
        (Join-Path $ProjectDir "env\Scripts\python.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $PythonExe = $c; break }
    }
    if (-not $PythonExe) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $PythonExe = $cmd.Source }
    }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    throw "python.exe not found. Pass one explicitly: -PythonExe 'C:\Python313\python.exe'"
}
Write-Host "Project dir : $ProjectDir"
Write-Host "Python      : $PythonExe"
Write-Host ""

# ---- read the schedule from the program itself ---------------------------------
$raw = & $PythonExe $RunPy --print-schedule
if ($LASTEXITCODE -ne 0 -or -not $raw) {
    throw "Failed to read schedule from run.py. Run it manually to see the error."
}

$entries = @()
foreach ($line in $raw) {
    $text = ([string]$line).Trim()
    if (-not $text) { continue }
    $parts = $text.Split("|")
    if ($parts.Length -lt 2) { continue }
    # only ASCII fields are used, so console codepage cannot break parsing
    $entries += [pscustomobject]@{ Id = $parts[0].Trim(); Time = $parts[1].Trim() }
}

if ($entries.Count -eq 0) {
    throw "No slots found in config.yaml (schedule.slots is empty)."
}

# ---- register one task per slot ------------------------------------------------
$days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$restartInterval = New-TimeSpan -Minutes 5
$execLimit = New-TimeSpan -Minutes 15

foreach ($e in $entries) {
    $taskName = "$TaskPrefix`_$($e.Id)"
    $at = [datetime]::ParseExact($e.Time, "HH:mm", $null)

    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "`"$RunPy`" --slot $($e.Id)" `
        -WorkingDirectory $ProjectDir

    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $at

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval $restartInterval `
        -ExecutionTimeLimit $execLimit `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Shanxi Coking Coal 000983 daily quote report slot=$($e.Id)" `
        -Force | Out-Null

    Write-Host ("[OK] {0}  ->  Mon-Fri {1}" -f $taskName, $e.Time)
}

Write-Host ""
Write-Host "Done. $($entries.Count) task(s) registered."
Write-Host "Verify with:  Get-ScheduledTask -TaskName '$TaskPrefix*' | Select TaskName,State"
Write-Host "Run now with: Start-ScheduledTask -TaskName '$TaskPrefix_$($entries[0].Id)'"
