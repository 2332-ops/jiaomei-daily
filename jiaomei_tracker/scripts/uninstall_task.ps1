<#
    Remove the Windows Scheduled Tasks created by install_task.ps1.
#>
param(
    [string]$TaskPrefix = "JiaomeiTracker"
)

$ErrorActionPreference = "Stop"

$tasks = Get-ScheduledTask -TaskName "$TaskPrefix*" -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Host "No tasks found with prefix '$TaskPrefix'."
    exit 0
}

foreach ($t in $tasks) {
    Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
    Write-Host "[REMOVED] $($t.TaskName)"
}
Write-Host ""
Write-Host "Done."
