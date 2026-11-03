"""Membership of close-labelled bars, including the A-share opening print.

The frozen QMT 5m bar ending at 09:35 includes the separate 09:30 1m
opening record. Other five-minute windows remain left-open/right-closed.
This is a source clock contract, not a change to Chan point definitions.
"""
from datetime import datetime
import re
from zoneinfo import ZoneInfo

CN = ZoneInfo('Asia/Shanghai')


def a_share_opening(snapshot, stamp, *, minute):
    if type(stamp) is not int or not re.fullmatch(r'(?:SH|SZ|BJ)\.\d{6}', str(snapshot.get('symbol', ''))):
        return False
    clock = datetime.fromtimestamp(stamp, CN)
    return (clock.hour, clock.minute, clock.second) == (9, minute, 0)


def contains(interval, stamp, *, pivot=False):
    left, right = ('pivot_after', 'pivot_through') if pivot else ('start_after', 'end_at')
    inclusive = interval.get('pivot_start_inclusive' if pivot else 'start_inclusive') is True
    return type(stamp) is int and (interval[left] <= stamp if inclusive else interval[left] < stamp) and stamp <= interval[right]


def minute_start(snapshot, stamp):
    return stamp if a_share_opening(snapshot, stamp, minute=30) else stamp - 60
