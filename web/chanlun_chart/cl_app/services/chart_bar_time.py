"""Authoritative labels for intraday US chart bars; never shift source times."""

_US_FREQUENCIES = frozenset({"1m", "5m", "30m"})
_LABELS = ("start", "end")


def chart_bar_time_fields(chart_data):
    label = chart_data.get("bar_time_label")
    if label is None:
        return {}
    if label not in ("start", "end"):
        raise ValueError("chart bar_time_label must be start or end")
    return {"bar_time_label": label}


def attach_chart_bar_time_label(frame, *, market, frequency, exchange):
    """Bind the actual producing adapter's convention to its displayed frame.

    Other markets and calendar bars retain their existing display contract.
    An adapter without a declaration is not guessed from the market name.
    """
    if market != "us" or frequency not in _US_FREQUENCIES or frame is None:
        return frame
    declared = getattr(exchange, "kline_time_label", None)
    existing = frame.attrs.get("bar_time_label")
    if existing is not None and existing not in _LABELS:
        raise ValueError("source bar_time_label must be start or end")
    if declared is None:
        return frame
    if declared not in _LABELS:
        raise ValueError("exchange kline_time_label must be start or end")
    if existing is not None and existing != declared:
        raise ValueError("source and exchange bar time labels differ")
    result = frame.copy(deep=False)
    result.attrs = dict(frame.attrs)
    result.attrs["bar_time_label"] = declared
    return result
