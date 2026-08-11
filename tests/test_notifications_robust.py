"""notifications 健壮性: DingTalkWebhookNotifier.send 对畸形 webhook(缺 scheme, 如粘贴丢了
https:// 前缀)必须按类契约 warning+返回 False, 不得让 urllib Request() 构造期的 ValueError
逃逸——逃逸会击穿 live_monitor 主循环(:2408 无 try 兜底)致常驻实盘进程盘中首个信号猝死,
且先于 broker.fill_pending/queue_events 交易循环同死(新R3-F1-NOTIF-3)。"""

from chanlun.notifications import DingTalkWebhookNotifier


class _DingTalkSuccess:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def read():
        return b'{"errcode": 0}'


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


def test_dry_run_injects_required_buy_sell_keyword():
    collected = []
    n = DingTalkWebhookNotifier(
        webhook="https://oapi.dingtalk.com/robot/send?access_token=redacted",
        keyword="买卖通知",
        dry_run=True,
        dry_run_collector=collected.append,
    )

    assert n.send("三类买点", ["SH.600000"]) is True

    assert "买卖通知" in collected[0]


def test_rich_notification_uses_one_markdown_payload_with_image(monkeypatch):
    import json
    import urllib.request

    requests = []

    def succeed(request, timeout):
        del timeout
        requests.append(request)
        return _DingTalkSuccess()

    monkeypatch.setattr(urllib.request, "urlopen", succeed)
    notifier = DingTalkWebhookNotifier(
        "https://example.invalid/send",
        keyword="买卖通知",
        rich_content_provider=lambda _context: [
            {
                "url": "http://47.96.40.233:8890/public/alert-chart/a.png?x=1",
                "alt": "SZ.000001 30分钟/5分钟/1分钟结构图",
            }
        ],
    )

    assert notifier.send_rich(
        "买卖通知｜持仓股｜SZ.000001｜1分钟三类买点",
        ["建议：回抽确认后考虑分批增持"],
        {"charts": [{"code": "SZ.000001"}]},
    ) is True

    assert len(requests) == 1
    payload = json.loads(requests[0].data.decode("utf-8"))
    assert payload["msgtype"] == "markdown"
    assert "买卖通知" in payload["markdown"]["text"]
    assert "![SZ.000001 30分钟/5分钟/1分钟结构图](http://" in payload[
        "markdown"
    ]["text"]


def test_rich_notification_chart_failure_falls_back_to_one_text(monkeypatch):
    import json
    import urllib.request

    requests = []

    def fail_chart(_context):
        raise RuntimeError("renderer unavailable")

    def succeed(request, timeout):
        del timeout
        requests.append(request)
        return _DingTalkSuccess()

    monkeypatch.setattr(urllib.request, "urlopen", succeed)
    monkeypatch.setattr(
        "chanlun.notifications.fun.get_logger",
        lambda: type("Logger", (), {"warning": lambda _self, _message: None})(),
    )
    notifier = DingTalkWebhookNotifier(
        "https://example.invalid/send",
        rich_content_provider=fail_chart,
    )

    assert notifier.send_rich("买卖通知", ["SZ.000001"], {}) is True

    assert len(requests) == 1
    payload = json.loads(requests[0].data.decode("utf-8"))
    assert payload["msgtype"] == "text"


def test_evidence_bound_chart_failure_blocks_the_notification(monkeypatch):
    import urllib.request

    requests = []

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: requests.append((request, timeout)),
    )
    monkeypatch.setattr(
        "chanlun.notifications.fun.get_logger",
        lambda: type("Logger", (), {"warning": lambda _self, _message: None})(),
    )
    notifier = DingTalkWebhookNotifier(
        "https://example.invalid/send",
        rich_content_provider=lambda _context: (_ for _ in ()).throw(
            RuntimeError("claimed marker absent")
        ),
    )

    assert notifier.send_rich(
        "买卖通知",
        ["TSLA.US"],
        {"require_evidence_match": True, "charts": [{"code": "TSLA.US"}]},
    ) is False
    assert requests == []


def test_persistent_outbound_gate_sends_identical_message_only_once(
    monkeypatch,
    tmp_path,
):
    import urllib.request

    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"errcode": 0}'

    def succeed(request, timeout):
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", succeed)
    state_path = tmp_path / "dingtalk-outbound.json"
    first = DingTalkWebhookNotifier(
        "https://example.invalid/send",
        keyword="买卖通知",
        dedupe_state_path=state_path,
    )

    assert first.send("买卖通知｜候选股", ["SZ.002335｜一分钟三买"]) is True
    assert first.send("买卖通知｜候选股", ["SZ.002335｜一分钟三买"]) is True
    restarted = DingTalkWebhookNotifier(
        "https://example.invalid/send",
        keyword="买卖通知",
        dedupe_state_path=state_path,
    )
    assert restarted.send("买卖通知｜候选股", ["SZ.002335｜一分钟三买"]) is True
    assert restarted.send("买卖通知｜候选股", ["SZ.002335｜另一触发"]) is True

    assert len(calls) == 2
    persisted = state_path.read_text(encoding="utf-8")
    assert "SZ.002335" not in persisted
    assert "一分钟三买" not in persisted


def test_failed_outbound_is_not_deduplicated_and_can_retry(monkeypatch, tmp_path):
    import urllib.request

    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"errcode": 0}'

    def fail_then_succeed(_request, timeout):
        nonlocal calls
        del timeout
        calls += 1
        if calls == 1:
            raise OSError("transient")
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fail_then_succeed)
    notifier = DingTalkWebhookNotifier(
        "https://example.invalid/send",
        dedupe_state_path=tmp_path / "dingtalk-outbound.json",
    )

    assert notifier.send("买卖通知", ["SZ.002335"]) is False
    assert notifier.send("买卖通知", ["SZ.002335"]) is True
    assert calls == 2


def test_network_failure_log_never_exposes_webhook_token(monkeypatch):
    import urllib.request

    warnings = []
    webhook = (
        "https://oapi.dingtalk.com/robot/send?access_token="
        "never-log-this-sensitive-token"
    )

    def fail(_request, timeout):
        del timeout
        raise RuntimeError(f"request failed for {webhook}")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    monkeypatch.setattr(
        "chanlun.notifications.fun.get_logger",
        lambda: type("Logger", (), {"warning": warnings.append})(),
    )

    assert DingTalkWebhookNotifier(webhook=webhook).send("t", ["x"]) is False
    assert warnings
    assert "never-log-this-sensitive-token" not in warnings[0]
