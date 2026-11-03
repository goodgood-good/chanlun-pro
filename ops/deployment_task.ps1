# Launch a deployment under Task Scheduler, independent of the calling shell.
. (Join-Path $PSScriptRoot 'no_console.ps1')
function Invoke-IndependentDeployment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$ScriptPath,
        [Parameter(Mandatory=$true)][string]$ProjectRoot,
        [Parameter(Mandatory=$true)][string[]]$WorkerArguments,
        [Parameter(Mandatory=$true)][int]$TimeoutSeconds
    )
    $taskName = 'ChanlunProDeployment-' + [guid]::NewGuid().ToString('N')
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $taskAction = New-NoConsoleTaskAction -ProjectRoot $ProjectRoot -Label $taskName -FilePath 'powershell.exe' `
        -Argument ($WorkerArguments -join ' ') -WorkingDirectory $ProjectRoot
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Seconds ($TimeoutSeconds + 300)) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Principal $principal -Settings $settings `
        -Description ('Independent chanlun deployment: ' + $ScriptPath) -Force | Out-Null
    $requestedAt = Get-Date
    try {
        Start-ScheduledTask -TaskName $taskName
        $deadline = $requestedAt.AddSeconds($TimeoutSeconds + 300)
        while ((Get-Date) -lt $deadline) {
            $task = Get-ScheduledTask -TaskName $taskName
            $info = Get-ScheduledTaskInfo -TaskName $taskName
            if ($info.LastRunTime -ge $requestedAt.AddSeconds(-1) -and [string]$task.State -eq 'Ready') {
                return [int]$info.LastTaskResult
            }
            Start-Sleep -Milliseconds 500
        }
        throw ('Independent deployment is still running; inspect ops/logs. Task: ' + $taskName)
    } finally {
        # Completion was observed before removal. Never stop an in-flight
        # deployment, or attach its long-lived descendants to the caller job.
        $remaining = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($remaining -and [string]$remaining.State -ne 'Running') {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
}
