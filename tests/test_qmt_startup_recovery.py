"""Verify quote readiness and the installed recovery schedule without live clients."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('qmt_quote_probe', ROOT / 'ops/check_qmt_market_data.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize('stocks,ready', [([], False), (['600000.SH'], False),
    (['600000.SH', '000001.SZ', '920001.BJ'], True),
    (['600000.SH', '000001.SZ', '920001.BJ', None], False)])
def test_rpc_without_complete_catalog_is_not_ready(tmp_path, stocks, ready):
    native = Mock()
    native.get_data_dir.return_value = str(tmp_path)
    native.get_stock_list_in_sector.return_value = stocks
    result = probe.inspect(native, tmp_path, 58610)
    assert result['ready'] is ready
    native.connect.assert_called_once_with('127.0.0.1', 58610)


def test_wrong_installation_is_rejected_before_reading_its_catalog(tmp_path):
    native = Mock()
    native.get_data_dir.return_value = str(tmp_path / 'other')
    assert probe.inspect(native, tmp_path, 58610)['reason_code'] == 'QMT_DATA_DIRECTORY_MISMATCH'
    native.get_stock_list_in_sector.assert_not_called()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Task Scheduler')
def test_installer_registers_logon_premarket_and_recurring_recovery_without_restarting_web(tmp_path):
    script = tmp_path / 'verify.ps1'
    installer = str(ROOT / 'ops/install_app_autostart.ps1').replace("'", "''")
    script.write_text(r'''$ErrorActionPreference='Stop'
function New-ScheduledTaskAction {param($Execute,$Argument,$WorkingDirectory) @{execute=$Execute;arguments=$Argument}}
function New-ScheduledTaskTrigger {param([switch]$AtLogOn,$User,[switch]$Weekly,$WeeksInterval,$DaysOfWeek,$At,[switch]$Once,$RepetitionInterval)
    [pscustomobject]@{logon=[bool]$AtLogOn;weekly=[bool]$Weekly;days=$DaysOfWeek;clock=if($At){$At.ToString('HH:mm')}else{''};repeat=$RepetitionInterval.TotalSeconds;Delay=''}}
function New-ScheduledTaskPrincipal {param($UserId,$LogonType,$RunLevel) @{level=$RunLevel;logon=$LogonType}}
function New-ScheduledTaskSettingsSet {param([switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,[switch]$StartWhenAvailable,$MultipleInstances,$RestartCount,$RestartInterval,$ExecutionTimeLimit) @{instances=$MultipleInstances;available=[bool]$StartWhenAvailable}}
function Register-ScheduledTask {param($TaskName,$Action,$Trigger,$Principal,$Settings,$Description,[switch]$Force)
    $global:registration=@{name=$TaskName;action=$Action;triggers=$Trigger;principal=$Principal;settings=$Settings}}
function Start-ScheduledTask {param($TaskName) $global:started=$TaskName}
''' + f"& '{installer}' -QmtOnly -PremarketStart '08:20' | Out-Null\n" +
        '@{registration=$registration;started=$started}|ConvertTo-Json -Depth 6 -Compress\n', encoding='utf-8')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    task = actual['registration']
    assert actual['started'] == task['name']
    assert task['triggers'][0]['logon'] and task['triggers'][0]['Delay'] == 'PT10S'
    assert task['triggers'][1]['weekly'] and task['triggers'][1]['clock'] == '08:20'
    assert len(task['triggers'][1]['days']) == 5
    assert task['triggers'][2]['repeat'] == 60
    assert task['settings'] == {'instances': 'IgnoreNew', 'available': True}
    assert 'no-console-' in task['action']['execute']
    assert '--tree' not in task['action']['arguments']
    assert '-Action Ensure' in task['action']['arguments']
    assert task['principal'] == {'level': 'Limited', 'logon': 'Interactive'}
