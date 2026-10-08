"""Preserve unavailable quote changes without substituting a real zero."""
import math


def quote_price(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def quote_change_rate(price, reference, digits=2, *, positive_reference=True):
    price, reference = quote_price(price), quote_price(reference)
    if (price is None or reference is None or reference == 0
            or positive_reference and reference < 0):
        return None
    change = (price - reference) / reference * 100
    if not math.isfinite(change):
        return None
    return change if digits is None else round(change, digits)
