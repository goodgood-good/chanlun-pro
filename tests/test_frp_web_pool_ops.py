from pathlib import Path


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
