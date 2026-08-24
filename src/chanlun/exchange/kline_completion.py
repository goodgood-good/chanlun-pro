"""跨市场统一的已收盘 K 线边界处理。"""

from __future__ import annotations

import pandas as pd


def frequency_to_minutes(frequency: str) -> int | float | None:
    """返回秒级或分钟级周期的分钟宽度，其他周期返回 ``None``。"""

    value = str(frequency).strip().lower()
    if value.endswith("s"):
        try:
            return max(int(value[:-1]), 1) / 60.0
        except ValueError:
            return None
    if value.endswith("m"):
        try:
            return max(int(value[:-1]), 1)
        except ValueError:
            return None
    return None


def _timestamp_at(value: object | None, reference: pd.Timestamp) -> pd.Timestamp:
    """把观测时刻投影到行情标签的时区口径。"""

    if value is None:
        return (
            pd.Timestamp.now(tz=reference.tz)
            if reference.tz is not None
            else pd.Timestamp.now()
        )
    observed = pd.Timestamp(value)
    if pd.isna(observed):
        raise ValueError("as_of must be a valid datetime")
    if reference.tz is None:
        return observed.tz_localize(None) if observed.tz is not None else observed
    if observed.tz is None:
        return observed.tz_localize(reference.tz)
    return observed.tz_convert(reference.tz)


def drop_unclosed_last_bar(
    frame: pd.DataFrame,
    frequency: str,
    *,
    time_label: str = "start",
    as_of: object | None = None,
) -> pd.DataFrame:
    """返回在冻结观测时刻已经完整收盘的连续前缀。

    端点标签的收盘时刻就是标签本身；起点标签的收盘时刻为标签加一个周期。函数
    只从尾部裁剪，历史会话缺口不会导致已收盘 K 线被误删。显式传入 ``as_of``
    可以确保一次多周期计算即使跨过新的收盘边界，也始终使用同一因果截面。
    """

    if time_label not in {"start", "end"}:
        raise ValueError("time_label must be start or end")
    minutes = frequency_to_minutes(frequency)
    if minutes is None or frame is None or len(frame) == 0:
        return frame
    step = pd.Timedelta(minutes=minutes)
    end = len(frame)
    while end > 0:
        try:
            last = pd.Timestamp(frame["date"].iloc[end - 1])
            observed = _timestamp_at(as_of, last)
        except Exception:
            return frame
        completed_at = last if time_label == "end" else last + step
        if observed >= completed_at:
            break
        end -= 1
    return frame if end == len(frame) else frame.iloc[:end]


def normalize_completed_bar_labels(
    frame: pd.DataFrame,
    frequency: str,
    *,
    time_label: str,
) -> pd.DataFrame:
    """Return minute bars on one canonical completed-bar timestamp axis.

    Some providers label a bar by its opening boundary while QMT labels it by
    its completed boundary.  Structure timestamps cross frequencies, so the
    trading system must never mix the two conventions.  Callers first remove
    any unclosed tail using the provider convention, then normalize retained
    start labels to their completion time with this function.
    """

    if time_label not in {"start", "end"}:
        raise ValueError("time_label must be start or end")
    minutes = frequency_to_minutes(frequency)
    if frame is None or len(frame) == 0 or minutes is None or time_label == "end":
        return frame
    if "date" not in frame.columns:
        raise ValueError("bar frame requires a date column")
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any():
        raise ValueError("bar timestamps must be valid datetimes")
    normalized = frame.copy()
    normalized["date"] = dates + pd.Timedelta(minutes=minutes)
    normalized.attrs = dict(frame.attrs)
    return normalized


__all__ = (
    "drop_unclosed_last_bar",
    "frequency_to_minutes",
    "normalize_completed_bar_labels",
)
