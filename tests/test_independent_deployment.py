"""Exercise the actual deployment-task helper with isolated scheduler doubles."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name!='nt',reason='Windows Task Scheduler launcher')
def test_independent_launcher_uses_limited_hidden_task_and_removes_only_completed_registration(tmp_path):
    script=tmp_path/'check.ps1'
    helper=str(ROOT/'ops/deployment_task.ps1').replace("'","''")
    script.write_text('''$ErrorActionPreference='Stop'
$global:registered=$null;$global:removed=$null
function New-ScheduledTaskAction {param($Execute,$Argument,$WorkingDirectory) @{execute=$Execute;arguments=$Argument;root=$WorkingDirectory}}
function New-ScheduledTaskPrincipal {param($UserId,$LogonType,$RunLevel) @{logon=$LogonType;level=$RunLevel}}
function New-ScheduledTaskSettingsSet {param([switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,$ExecutionTimeLimit,$MultipleInstances) @{}}
function Register-ScheduledTask {param($TaskName,$Action,$Principal,$Settings,$Description,[switch]$Force) $global:registered=@{name=$TaskName;action=$Action;principal=$Principal}}
function Start-ScheduledTask {param($TaskName) $global:started=$TaskName}
function Get-ScheduledTask {param($TaskName,$ErrorAction) [pscustomobject]@{State='Ready'}}
function Get-ScheduledTaskInfo {param($TaskName) [pscustomobject]@{LastRunTime=(Get-Date);LastTaskResult=0}}
function Unregister-ScheduledTask {param($TaskName,$Confirm,$ErrorAction) $global:removed=$TaskName}
function Stop-ScheduledTask {throw 'must not terminate background descendants'}
function Start-Process {throw 'must not attach to the calling process job'}
''' + f". '{helper}'\n" + r'''$exit=Invoke-IndependentDeployment -ScriptPath 'D:\app\ops\restart_web.ps1' -ProjectRoot 'D:\app' -WorkerArguments @('-NoProfile','-WindowStyle','Hidden','-File','"D:\app\ops\restart_web.ps1"','-DeploymentWorker') -TimeoutSeconds 30
@{exit=$exit;registration=$registered;started=$started;removed=$removed} | ConvertTo-Json -Depth 5 -Compress
'''.replace('D:\\app', str(tmp_path/'application')),encoding='utf-8')
    result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],
                          capture_output=True,text=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    actual=json.loads(result.stdout.strip())
    assert actual['exit']==0
    assert actual['started']==actual['removed']==actual['registration']['name']
    assert actual['registration']['principal']=={'level':'Limited','logon':'Interactive'}
    assert '-WindowStyle Hidden' in actual['registration']['action']['arguments']
    assert '-DeploymentWorker' in actual['registration']['action']['arguments']
