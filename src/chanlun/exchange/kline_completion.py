"""Market-neutral completed K-line boundary handling."""

from __future__ import annotations

import pandas as pd


def frequency_to_minutes(frequency: str) -> int | None:
    """Return a positive minute width, or ``None`` for non-minute levels."""

    value = str(frequency).strip().lower()
    if not value.endswith("m"):
        return None
    try:
        return max(int(value[:-1]), 1)
    except ValueError:
        return None


def drop_unclosed_last_bar(
    frame: pd.DataFrame,
    frequency: str,
    *,
    time_label: str = "start",
) -> pd.DataFrame:
    """Remove only a terminal minute bar whose period is not complete yet.

    Session gaps are deliberately conservative: a historical bar is never
    removed merely because the final two timestamps are not one nominal
    interval apart.
    """

    if time_label not in {"start", "end"}:
        raise ValueError("time_label must be start or end")
    minutes = frequency_to_minutes(frequency)
    if minutes is None or frame is None or len(frame) == 0:
        return frame
    try:
        last = pd.Timestamp(frame["date"].iloc[-1])
    except Exception:
        return frame
    now = pd.Timestamp.now(tz=last.tz) if last.tz is not None else pd.Timestamp.now()
    if time_label == "end":
        return frame.iloc[:-1] if now < last else frame
    if len(frame) < 2:
        return frame
    try:
        previous = pd.Timestamp(frame["date"].iloc[-2])
    except Exception:
        return frame
    step = pd.Timedelta(minutes=minutes)
    if last - previous != step:
        return frame.iloc[:-1] if now < last else frame
    return frame.iloc[:-1] if now < last + step else frame


__all__ = ("drop_unclosed_last_bar", "frequency_to_minutes")
