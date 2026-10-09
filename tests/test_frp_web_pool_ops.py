from pathlib import Path
import json
import os
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_frp_pool_template_is_grouped_and_keeps_credentials_out_of_source():
    template = (ROOT / "ops/frpc_web_pool.toml").read_text(encoding="utf-8")

    assert 'auth.token = "{{ .Envs.CHANLUN_FRP_POOL_TOKEN }}"' in template
    assert 'remotePort = 8890' in template
    assert 'transport.protocol = "tcp"' in template
    assert template.count('loadBalancer.group = "chanlun-web-pool-v1"') == 1
    assert template.count('loadBalancer.groupKey = "chanlun-web-pool-v1"') == 1


def test_frp_pool_supervisor_is_single_flight_and_uses_hidden_child_processes():
    source = (ROOT / "ops/watch_frp_web_pool.ps1").read_text(encoding="utf-8")

    assert "Local\\ChanlunProFrpWebPool" in source
    assert "Diagnostics.ProcessStartInfo" in source
    assert "$startInfo.UseShellExecute = $false" in source
    assert "$startInfo.CreateNoWindow = $true" in source
    assert "CHANLUN_FRP_POOL_TOKEN" in source
    assert "auth\\.token" in source
    assert "Start-Process" not in source


def test_frp_pool_installer_is_user_scoped_hidden_and_restartable():
    source = (ROOT / "ops/install_frp_web_pool.ps1").read_text(encoding="utf-8")

    assert '"ChanlunFrpWebPool-$scopeHash"' in source
    assert "-AtLogOn -User $identity" in source
    assert "-LogonType Interactive" in source
    assert "-RunLevel Limited" in source
    assert "'-WindowStyle Hidden'" in source
    assert "-RestartCount 3" in source
    assert "Start-ScheduledTask -TaskName $taskName" in source


@pytest.mark.skipif(os.name != "nt", reason="Windows scheduled-task installer")
def test_frp_installer_registers_periodic_single_instance_recovery_without_live_changes():
    # Override all ScheduledTasks cmdlets before executing the real installer.
    # Neither a task nor a tunnel is created by this offline registration test.
    project = str(ROOT).replace("'", "''")
    installer = str(ROOT / "ops/install_frp_web_pool.ps1").replace("'", "''")
    script = r'''
$ErrorActionPreference = 'Stop'
$global:FrpTestStarts = 0
function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}
}
function New-ScheduledTaskTrigger {
    param([switch]$AtLogOn, [switch]$Once, $User, $At, [TimeSpan]$RepetitionInterval)
    [pscustomobject]@{Kind=$(if($AtLogOn){'logon'}else{'once'}); Seconds=$RepetitionInterval.TotalSeconds}
}
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{LogonType=$LogonType; RunLevel=$RunLevel}
}
function New-ScheduledTaskSettingsSet {
    param([switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,
          [switch]$StartWhenAvailable,$MultipleInstances,$ExecutionTimeLimit,$RestartCount,$RestartInterval)
    [pscustomobject]@{StartWhenAvailable=[bool]$StartWhenAvailable; MultipleInstances=$MultipleInstances;
                     RestartCount=$RestartCount; Unlimited=($ExecutionTimeLimit.TotalSeconds -eq 0)}
}
function Register-ScheduledTask {
    param($TaskName,$Action,$Trigger,$Principal,$Settings,[switch]$Force)
    $global:FrpTestRegistration=[pscustomobject]@{Name=$TaskName; Action=$Action; Triggers=@($Trigger);
                                        Principal=$Principal; Settings=$Settings}
}
function Start-ScheduledTask { param($TaskName) $global:FrpTestStarts += 1 }
function Get-ScheduledTask { param($TaskName) [pscustomobject]@{State='Ready'} }
& '__INSTALLER__' -ProjectRoot '__PROJECT__' -PoolSize 3 -PollSeconds 10 | Out-Null
[pscustomobject]@{Registration=$global:FrpTestRegistration; Starts=$global:FrpTestStarts} | ConvertTo-Json -Depth 6 -Compress
'''.replace("__INSTALLER__", installer).replace("__PROJECT__", project)
    executable = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    task = value["Registration"]
    assert task["Name"].startswith("ChanlunFrpWebPool-")
    assert task["Triggers"] == [{"Kind": "logon", "Seconds": None}, {"Kind": "once", "Seconds": 300}]
    assert task["Settings"]["MultipleInstances"] == "IgnoreNew"
    assert task["Settings"]["StartWhenAvailable"] is True
    assert task["Settings"]["Unlimited"] is True
    assert task["Principal"] == {"LogonType": "Interactive", "RunLevel": "Limited"}
    assert task["Action"]["WorkingDirectory"] == str(ROOT)
    assert "-PoolSize 3" in task["Action"]["Arguments"]
    assert "-PollSeconds 10" in task["Action"]["Arguments"]
    assert "-WindowStyle Hidden" in task["Action"]["Arguments"]
    assert value["Starts"] == 1
