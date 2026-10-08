"""Concise notification presentation; all prices come from supplied evidence."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
import re
from urllib.parse import urlsplit

from chanlun import config
from chanlun.screening.rules import CN

MARKETS = {"a": "A股", "us": "美股", "currency_spot": "现货", "fx": "外汇", "ny_futures": "期货"}
POINTS = {"1buy": "一买", "2buy": "二买", "3buy": "三买", "1sell": "一卖", "2sell": "二卖", "3sell": "三卖"}
STAGES = {"warning": "提前预警（未确认）", "invalidated": "预警已失效", "confirmed": "已确认",
          "approaching": "形成等待（未确认）", "formed": "形成等待（未确认）", "observed": "形成等待（未确认）"}
REASONS = {
    "PRICE_EXTREME_BROKEN": "价格突破了原候选极值，原局部转折不再成立",
    "LOCAL_TURN_PENDING": "当前端点尚未形成已收盘行情支持的局部转折",
    "LOCAL_TURN_CHANGED": "端点附近价格结构变化，局部转折条件已不成立",
    "MOMENTUM_CONTRACTION_LOST": "同向MACD柱不再满足连续两次收缩条件",
    "MOMENTUM_RECOVERED": "同向MACD柱在收缩后重新扩大",
    "AREA_MARGIN_LOST": "当前面积对比已不满足提前预警门槛",
    "OPEN_SEGMENT_CHANGED": "原未完成段的状态或划分已更新",
    "COMPARISON_CHANGED": "中枢或比较段已改变，原比较依据不再适用",
    "COMPARISON_UNAVAILABLE": "当前结构缺少有效的同向比较依据",
    "PRICE_BASIS_CHANGED": "价格基准已更新，原比较依据需要重新建立",
    "DIAGNOSTIC_UNAVAILABLE": "已核实预警条件不再成立，但本次未提供具体触发项",
}


def clean(value, limit=100):
    return " ".join(str(value).split())[:limit]


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def price(value, quantum=None):
    value = number(value)
    if value is None:
        return "—"
    try:
        q = Decimal(str(quantum))
        if not q.is_finite() or q <= 0:
            raise ValueError("invalid precision")
        places = max(0, -q.normalize().as_tuple().exponent)
        if places > 12:
            return f"{value:.8g}"
        rendered = f"{value:,.{places}f}"
        if places > 2:
            whole, fraction = rendered.split(".")
            rendered = whole + "." + fraction.rstrip("0").ljust(2, "0")
        return rendered
    except (ValueError, TypeError, InvalidOperation):
        return f"{value:,.8f}".rstrip("0").rstrip(".")


def date(value):
    return datetime.fromtimestamp(value, CN).strftime("%m-%d %H:%M:%S") if value else "—"


def elapsed(start, end):
    seconds = max(0, int(end - start))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (f"{hours}小时" if hours else "") + (f"{minutes}分" if minutes else "") + (f"{seconds}秒" if seconds or not (hours or minutes) else "")


def public_base():
    raw = str(config.ALERT_CHART_PUBLIC_BASE_URL or "").strip().rstrip("/")
    try:
        u = urlsplit(raw)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            return ""
    except ValueError:
        return ""
    return raw


def heading(row):
    if row.get('signal_type')=='holding_risk':
        kind='结构止损触发' if row['point_type']=='risk_stop' else '反向信号确认'
        return f"【持仓风险】{clean(row.get('name') or row['code'],28)} {clean(row['code'],80)} · {row['frequency']} · {kind}"
    state = row.get("point_status", row.get("stage"))
    tag = {"warning": "提前预警", "invalidated": "预警失效", "confirmed": "已确认"}.get(state, STAGES.get(state, "监听更新"))
    if state == 'warning' and row.get('warning_reactivated'):
        tag = '预警恢复'
    code = clean(row.get("code", ""), 80)
    name = clean(row.get("name") or code, 28)
    label = name if name == code else f"{name} {code}"
    kind = POINTS.get(row.get("point_type"), "上涨段盘整背驰" if row.get("point_type") == "consolidation_up" else "下跌段盘整背驰")
    return f"【{tag}】{label} · {row['frequency']} · {kind}"


def signal_status_lines(row):
    status = row.get("point_status")
    label = STAGES.get(status, status) if status is not None else ("已确认" if row["stage"] == "confirmed" else "筛选条件待齐")
    lines = [f"{row['frequency']} {POINTS.get(row['point_type'], row['point_type'])} · {label}"]
    if row.get("signal_type") == "consolidation_divergence":
        direction = "向上" if row["point_type"] == "consolidation_up" else "向下"
        lines = [f"{row['frequency']} {direction}盘整背驰 · {label} · 持仓标的"]
        if row.get("stage") == "invalidated":
            lines.append("失效原因：" + (row.get("invalidation_reason") or REASONS["DIAGNOSTIC_UNAVAILABLE"]))
        else:
            lines.append("比较范围：" + ("中枢进入与离开" if row.get("comparison_form") == "entry_departure" else "中枢内同向摆动"))
        ratio = number(row.get("macd_area_ratio"))
        if ratio is None:
            a, c = number(row.get("macd_area_a")), number(row.get("macd_area_c"))
            ratio = c/a if a and c is not None else None
        if ratio is not None and row.get("stage") != "invalidated":
            lines.append(f"MACD 同向柱面积比 C/A：{ratio:.1%}")
        if row.get("stage") == "warning":
            lines += ["依据：面积偏弱、动能连续收缩及局部价格转折", "线段尚未完成，预警可被后续走势推翻。"]
        elif row.get("stage") == "invalidated":
            lines.append("原预警已撤销，不代表新的买卖点。")
    lower = row.get("lower_confirmation_state")
    if lower is not None:
        lines.append(f"{row.get('lower_frequency') or '1m'} 区间内结构确认：" + ("已确认" if lower == "confirmed" else "等待确认"))
    following = row.get("following_segment_state")
    if following in {"in_progress", "completed"}:
        direction = {"up": "向上", "down": "向下"}.get(row.get("following_segment_direction"), "")
        lines.append(f"后继{direction}线段：" + ("进行中（尚未完成）" if following == "in_progress" else "已完成"))
    return lines


def signal_body(row):
    if row.get('signal_type')=='holding_risk':
        q=row.get('structure_price_quantum')
        current_q=row.get('current_price_quantum',q)
        lines=['我的持仓 · 已绑定的参考买点',f"参考：{row['frequency']} {POINTS.get(row.get('reference_point_type'),'买点')} · {price(row.get('anchor_price'),q)}"]
        if row['point_type']=='risk_stop':
            lines += [f"已收盘K线曾跌破结构止损参考 {price(row.get('stop_price'),q)}",
                      f"触发K线最低价：{price(row.get('trigger_price'),current_q)}"]
        else:
            lines += [f"出现已确认{POINTS.get(row.get('reverse_point_type'),'卖点')}，参考价 {price(row.get('reverse_price'),q)}"]
        lines += [('触发K线收盘：' if row['point_type']=='risk_stop' else '反向信号确认：')+date(row.get('available_at')),
                  '最新收盘：'+price(row.get('latest_close'),current_q),'行情截止：'+date(row.get('source_closed_at'))+'（北京时间）']
        return lines
    quantum = row.get("structure_price_quantum")
    phase = row.get("stage")
    anchor_label = "候选拐点" if phase == "warning" else "原预警参考价" if phase == "invalidated" else "结构拐点"
    lines = [MARKETS.get(row.get("market", "a"), row.get("market", "")) + (" · 我的持仓" if row.get("holding") else ""),
             *signal_status_lines(row), f"{anchor_label}：{price(row.get('anchor_price'), quantum)}"]
    last, anchor = number(row.get("latest_close")), number(row.get("anchor_price"))
    if last is not None:
        distance = ""
        if anchor and anchor > 0 and row.get('current_price_basis_revision') in (None,row.get('price_basis_revision')):
            delta = (last/anchor-1)*100
            places = 3 if 0 < abs(delta) < .01 else 2
            distance = f"（较参考价{delta:+.{places}f}%）"
        lines.append(f"最新{row['frequency']}收盘：{price(last, row.get('current_price_quantum') or quantum)}{distance}")
    if row.get("signal_type") != "consolidation_divergence":
        plan = row.get("exit_plan") or {}
        stop, target = plan.get("stop_loss") or {}, plan.get("take_profit") or {}
        if number(stop.get("price")) is not None and stop.get("status") == "reference":
            trigger = {"lt": "跌破", "gt": "突破"}.get(stop.get("trigger"), "")
            lines.append(f"结构止损参考：{trigger}{price(stop['price'], quantum)}")
        else:
            lines.append("结构止损参考：当前无可核验价格")
        if target.get("status") == "exit_confirmed" and number(target.get("price")) is not None:
            lines.append(f"反向退出信号：{POINTS.get(target.get('point_type'), '反向点')} · {price(target['price'], quantum)}")
        else:
            lines.append("止盈/退出：等待本周期已确认反向信号")
    first = row.get("first_warned_at")
    changed = row.get("state_changed_at") or row.get("recorded_at")
    if first:
        prefix = "首次预警" if phase == "warning" else "对应预警"
        suffix = f"（历时{elapsed(first, changed)}）" if phase != "warning" and changed and changed >= first else ""
        lines.append(f"{prefix}：{date(first)}{suffix}")
    if phase == "confirmed" and row.get("point_confirmed_at"):
        lines.append("确认时间：" + date(row["point_confirmed_at"]))
    if phase == "invalidated":
        lines.append("失效复核：" + date(row.get("invalidation_checked_at", row.get("source_closed_at"))))
    lines.append("行情截止：" + date(row.get("source_closed_at")) + "（北京时间）")
    return lines


def signal_content(events, stamp):
    title = heading(events[0]) if len(events) == 1 else f"【监听更新】{len(events)} 条信号"
    lines = [title]
    for row in events:
        lines += ["", *([heading(row)] if len(events) > 1 else []), *signal_body(row)]
    lines.append("消息生成：" + date(stamp))
    return "\n".join(lines)


def illustrated_signal(event, url, stamp):
    """One frozen event and its matching inline PNG, with no navigation links."""
    escape = lambda value: re.sub(r"([\\`*_{}\[\]()#+.!|>])", r"\\\1", str(value))
    title = heading(event)
    body = '\n\n'.join(escape(line) for line in signal_body(event))
    return {'title':title,'text':'### '+escape(title)+'\n\n'+body+
            '\n\n![信号快照]('+url+')\n\n消息生成：'+date(stamp)}
