"""Explain notification silence without manufacturing a new trading event."""


def describe(state, delivery, scheduler):
    charts = delivery.get("chart_delivery") or {}
    generation = delivery.get("chart_generation") or {}
    error = delivery.get("last_error") or generation.get("last_error") or ""
    counts = {
        "last_check_at": state.get("last_check_at"),
        "last_event_at": state.get("last_event_at"),
        "last_sent_at": delivery.get("last_sent_at"),
        "new_events": state.get("last_check_notifications"),
    }
    if delivery.get("configured") is False:
        key, label = "unconfigured", "尚未配置通知机器人"
    elif not state.get("dingtalk_enabled"):
        key, label = "disabled", "外部通知已关闭"
    elif scheduler in {"stopped", "delayed"}:
        key, label = "unhealthy", "监听运行异常，正在等待恢复"
    elif error and not error.startswith("等待"):
        key, label = "failed", "通知处理遇到问题：" + error
    elif state.get("worker_error"):
        key, label = "unhealthy", state["worker_error"]
    elif generation.get("pending", 0):
        key, label = "rendering", "已有通知，正在生成对应图表"
    elif (
        delivery.get("pending", 0)
        or charts.get("pending", 0)
        or delivery.get("awaiting_enqueue", 0)
    ):
        key, label = "pending", error or "已有通知，正在排队发送"
    elif not state.get("enabled"):
        key, label = "paused", "自动监听已暂停"
    elif state.get("pool_size") == 0:
        key, label = "empty", "监听名单为空，暂无可检查的标的"
    elif state.get("check_error"):
        key, label = "unhealthy", state["check_error"]
    elif counts["last_check_at"] is None:
        key, label = "starting", "正在等待首次监听检查"
    elif counts["new_events"]:
        receipts = state.get("receipts") or {}
        complete = receipts.get("available") and receipts.get("expected") == counts["new_events"]
        sent = receipts.get("sent", 0) if complete else 0
        if complete and sent == counts["new_events"]:
            key, label = "sent", "最近复核产生的新通知已发送"
        elif sent:
            key, label = "partial", f"最近新增事件已发送 {sent} / {counts['new_events']} 个，其余无待发送通知"
        elif receipts.get("available") is False:
            key, label = "recorded", "最近新增事件已记录，发送记录暂时无法核对"
        else:
            key, label = "recorded", "最近新增事件已记录，当前无待发送通知"
    else:
        key, label = "quiet", "监听运行中，最近复核没有新增可发送事件"
    return {**counts, "state": key, "message": label}
