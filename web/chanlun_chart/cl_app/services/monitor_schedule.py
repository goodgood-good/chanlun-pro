"""Describe daily screening using the same verified A-share calendar as scheduling."""

from chanlun.screening.rules import CN
from chanlun.exchange.trading_session import official_trading_session_evidence


def describe(state, manual, observed_at):
    now = observed_at.astimezone(CN)
    session = now.date().isoformat()
    settings = state.get("settings", {})
    daily = state.get("daily") or {}
    phases = {
        key: (daily.get(key) or {}) if daily.get("session") == session else {}
        for key in ("morning", "afternoon")
    }
    calendar_status, next_session = "unavailable", None
    try:
        evidence = official_trading_session_evidence(
            session=now.date(), observed_at=now
        )
        if evidence is not None and not state.get("daily_schedule_error"):
            calendar_status = {
                "TRADING_SESSION": "trading",
                "NON_TRADING_SESSION": "closed",
            }.get(evidence["classification"], "unavailable")
            next_session = next(
                (
                    day
                    for day in evidence["calendar_document"]["trading_days"]
                    if day > session
                ),
                None,
            )
    except (OSError, ValueError, KeyError, TypeError):
        pass

    enabled = bool(state.get("enabled"))
    completed = sum(phase.get("status") == "completed" for phase in phases.values())
    failed = any(
        phase.get("status") in {"failed", "cancelled", "interrupted"}
        for phase in phases.values()
    )
    exhausted = any(
        phase.get("status") != "completed" and phase.get("attempts", 0) >= 3
        for phase in phases.values()
    )
    if manual.get("status") in {"starting", "running"}:
        status, title = (
            "running",
            f"选股中 · {manual.get('completed', 0)} / {manual.get('total') or '—'}",
        )
    elif not enabled:
        status, title = "paused", "自动选股已暂停"
    elif failed:
        status, title = "attention", "本日选股需要检查"
    elif completed == 2:
        status, title = "completed", "今日两轮已完成"
    elif any(phase.get("status") == "started" for phase in phases.values()):
        status, title = "running", "今日选股进行中"
    elif calendar_status == "unavailable":
        status, title = "calendar_unavailable", "交易日历待核验"
    elif calendar_status == "closed":
        status, title = "non_trading_day", "今日无定时选股"
    elif phases["morning"].get("status") == "completed":
        status, title = "partial", "上午已完成，下午待运行"
    elif completed:
        status, title = "partial", "今日已完成 1 / 2 轮"
    else:
        status, title = "waiting", "今日待运行"

    def phase_text(key, label, clock):
        phase = phases[key]
        phase_status = phase.get("status")
        if phase_status == "completed":
            text = "已完成"
        elif phase_status == "started":
            text = "计算中"
        elif phase_status in {"failed", "cancelled", "interrupted"}:
            text = (
                "失败，已达重试上限"
                if phase.get("attempts", 0) >= 3
                else "失败，待重试"
            )
        elif not enabled:
            text = "已暂停"
        elif calendar_status == "closed":
            text = "本日不安排"
        elif calendar_status == "unavailable":
            text = "待核验"
        elif key == "morning" and phases["afternoon"].get("status") == "completed":
            text = "未运行"
        else:
            text = "待运行"
        return f"{label} {clock}：{text}"

    detail = (
        "；".join(
            (
                phase_text(
                    "morning", "上午", settings.get("morning_after_close", "11:35")
                ),
                phase_text("afternoon", "下午", settings.get("after_close", "15:10")),
            )
        )
        + "。"
    )
    if calendar_status == "closed" and enabled:
        detail += (
            f"下一 A 股交易日：{next_session.replace('-', '/')}。"
            if next_session
            else "后续安排等待交易日历更新。"
        )
    elif calendar_status == "unavailable" and enabled:
        detail += "定时选股等待交易日历核验。"
    elif enabled and completed < 2:
        detail += "应用需保持运行。"
    if exhausted:
        detail += "请检查行情后手动重新选股。"
    if manual.get("error"):
        detail += str(manual["error"])
    return {
        "status": status,
        "title": title,
        "detail": detail,
        "session": session,
        "calendar_status": calendar_status,
        "next_trading_session": next_session,
        "completed_phases": completed,
    }
