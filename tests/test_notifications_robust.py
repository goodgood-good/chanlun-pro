"""notifications 健壮性: DingTalkWebhookNotifier.send 对畸形 webhook(缺 scheme, 如粘贴丢了
https:// 前缀)必须按类契约 warning+返回 False, 不得让 urllib Request() 构造期的 ValueError
逃逸——逃逸会击穿 live_monitor 主循环(:2408 无 try 兜底)致常驻实盘进程盘中首个信号猝死,
且先于 broker.fill_pending/queue_events 交易循环同死(新R3-F1-NOTIF-3)。"""

from chanlun.notifications import DingTalkWebhookNotifier


def test_send_malformed_webhook_missing_scheme_returns_false():
    # 缺 https:// → urllib Request() 构造期抛 ValueError('unknown url type')
    # 修复前该异常在 try 之外逃逸 send();修复后纳入 try → 优雅降级返回 False(不 raise)
    n = DingTalkWebhookNotifier(webhook="oapi.dingtalk.com/robot/send?access_token=x")
    result = n.send("缠论实时买卖点提醒", ["买点 SH.600000"])
    assert result is False


def test_send_empty_webhook_returns_false():
    # 空 webhook 早退, 契约一致
    n = DingTalkWebhookNotifier(webhook="")
    assert n.send("t", ["x"]) is False


def test_dry_run_returns_true_no_network():
    # dry_run 直接打印返回 True, 不触网(健壮路径 sanity)
    n = DingTalkWebhookNotifier(webhook="oapi.dingtalk.com/x", dry_run=True)
    assert n.send("t", ["x"]) is True

def test_iter_hook_commands_malformed_degrades_empty():
    """F1-NOTIF-4: settings.json 合法 JSON 但畸形结构(hooks/Notification=null 或非预期类型)
    必须降级空, 不得抛 AttributeError/TypeError 逃逸 discover→ClaudeHookNotifier.__init__→监控崩。"""
    from chanlun.notifications import _iter_hook_commands

    assert list(_iter_hook_commands({"hooks": None}, "Notification")) == []
    assert list(_iter_hook_commands({"hooks": {"Notification": None}}, "Notification")) == []
    assert list(_iter_hook_commands({"hooks": {"Notification": {"x": 1}}}, "Notification")) == []
    assert list(_iter_hook_commands(None, "Notification")) == []
    assert list(_iter_hook_commands({"hooks": {"Notification": [None, "s", {}]}}, "Notification")) == []


def test_iter_hook_commands_valid_returns_command():
    from chanlun.notifications import _iter_hook_commands

    settings = {"hooks": {"Notification": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]}}
    assert list(_iter_hook_commands(settings, "Notification")) == ["notify.sh"]


def test_discover_malformed_settings_returns_none(tmp_path):
    from chanlun.notifications import discover_claude_notification_command

    p = tmp_path / "settings.json"
    p.write_text('{"hooks": null}', encoding="utf-8")  # 合法 JSON 但 hooks=null
    assert discover_claude_notification_command(settings_path=str(p)) is None