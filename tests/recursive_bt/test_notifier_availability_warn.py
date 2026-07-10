"""F1-NOTIF-2: 通知通道完全缺失(未配 dingtalk_webhook 且未发现 Claude Notification hook)时
live_monitor/app_monitor 照常启动零 fail-fast, Notifier.available 属性历来零消费, 监控静默运行
=用户误以为被实时提醒(paper 照常下单)。warn_if_notifier_unavailable 补一次启动 [ALERT] 告警
(纯可观测, 不改交易行为)(新R3-F1-NOTIF-2)。"""

from types import SimpleNamespace

from chanlun.recursive_bt.monitor.live_monitor import warn_if_notifier_unavailable


def _capturing_log():
    msgs = []
    return SimpleNamespace(warning=lambda m: msgs.append(m)), msgs


def test_warns_when_unavailable_not_dry_run():
    log, msgs = _capturing_log()
    warned = warn_if_notifier_unavailable(SimpleNamespace(available=False), dry_run=False, log=log)
    assert warned is True
    assert msgs and "[monitor][ALERT]" in msgs[0] and "通知通道不可用" in msgs[0]


def test_no_warn_when_available():
    log, msgs = _capturing_log()
    assert warn_if_notifier_unavailable(SimpleNamespace(available=True), dry_run=False, log=log) is False
    assert msgs == []


def test_no_warn_in_dry_run():
    log, msgs = _capturing_log()
    assert warn_if_notifier_unavailable(SimpleNamespace(available=False), dry_run=True, log=log) is False
    assert msgs == []


def test_missing_available_attr_treated_unavailable():
    log, msgs = _capturing_log()
    assert warn_if_notifier_unavailable(SimpleNamespace(), dry_run=False, log=log) is True
    assert msgs


def test_real_dingtalk_empty_webhook_warns():
    from chanlun.notifications import DingTalkWebhookNotifier

    log, msgs = _capturing_log()
    n = DingTalkWebhookNotifier(webhook="")  # available = bool("") or False = False
    assert warn_if_notifier_unavailable(n, dry_run=False, log=log) is True
    assert msgs


def test_real_claude_hook_no_command_warns(tmp_path):
    from chanlun.notifications import ClaudeHookNotifier

    log, msgs = _capturing_log()
    # settings 指向不存在文件 + 无显式 command → 若 discover 不到则 available False
    n = ClaudeHookNotifier(command=None, settings_path=str(tmp_path / "nope.json"))
    warned = warn_if_notifier_unavailable(n, dry_run=False, log=log)
    # 环境无关断言: warned 与 available 严格互反
    assert warned == (not n.available)