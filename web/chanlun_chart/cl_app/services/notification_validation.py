"""Bound dependency reads without abandoning a task that can change signals.

Only read-only dependency functions belong here. A completed read can resume
the same request briefly; the caller alone may inspect state and authorize it.
"""

from concurrent.futures import Future
import threading
import time


VALIDATION_BUDGET_SECONDS = .2
COMPLETED_RESULT_TTL_SECONDS = 1
MAX_DEPENDENCY_AGE_SECONDS = 2


class DeferredEvents(list):
    """A read budget yielded; retry sooner than a genuinely unknown proof."""


def retry_delay(events):
    return .5 if isinstance(events, DeferredEvents) else 5


def validation_deadline():
    return time.monotonic() + VALIDATION_BUDGET_SECONDS


def remaining(deadline):
    return max(0, deadline - time.monotonic())


def state_epoch(state, control_version, events):
    # This epoch belongs to the lane, so two different held symbols share it.
    # Event-specific admission times are still checked against live state.
    membership = (tuple(sorted(state.get('holding_symbols', []))),
                  tuple(sorted(state.get('holding_notification_since', {}).items())))
    return (control_version, state.get('enabled'), state.get('dingtalk_enabled'),
            state.get('notification_since'), state.get('holding_source_current'), membership,
            tuple(sorted(state.get('notification_frequency_since', {}).items())),
            tuple(sorted((state.get('seed') or {}).get('settings', {}).get('point_types', []))),
            state.get('consolidation_enabled_at'), state.get('consolidation_warning_enabled_at'),
            state.get('point_notification_version'),
            tuple(sorted((scope, tuple(values)) for scope, values in state.get('point_notifications', {}).items())),
            tuple(sorted(state.get('point_notification_since', {}).items())))


REJECTION_LABELS = {
    'MARKET_EVENT_EXPIRED':'原市场事件已超过通知时限，未补发',
    'INVALID_MARKET_CLOCK':'通知缺少有效市场时间，未发送',
    'FUTURE_MARKET_CLOCK':'通知时间晚于当前市场时刻，未发送',
    'NOTIFICATION_POLICY_DISABLED':'当前通知偏好已关闭该类信号，未发送',
    'TIMEFRAME_CONFIRMATION_REQUIRED':'交易级别或下级确认条件不符，未发送',
    'HOLDING_REMOVED':'标的已不属于适用持仓范围，未发送',
    'OUTSIDE_MONITOR_SCOPE':'标的或周期已不在监听范围，未发送',
    'BEFORE_NOTIFICATION_ENABLED':'信号早于通知启用时刻，未补发',
    'SIGNAL_EXIT_CONFIRMED':'当前证据已确认原信号退出，未发送',
    'SIGNAL_SUPERSEDED':'原通知已被后续信号状态替代，未发送',
    'SIGNAL_STATE_CHANGED':'原通知状态与当前信号不一致，未发送',
    'SIGNAL_IDENTITY_REJECTED':'当前已核验证据不包含原信号身份，未发送',
    'FIRST_CENTER_POLICY_REJECTED':'三买不满足当前首中枢筛选条件，未发送',
    'MULTIPLE_RECHECK_REJECTIONS':'本组信号因不同复核条件被排除，原因已逐条保存',
    'PARTIAL_RECHECK_REJECTIONS':'部分信号已排除，其他信号继续按复核结果处理',
    'DUPLICATE_SIGNAL_SUPPRESSED':'同一结构、价格和状态已通知，本次重复信号已合并',
    'RECHECK_REJECTED':'当前信号复核未通过，未发送',
}


class ValidatedEvents(list):
    """Keep terminal validation reasons without changing the two-list API."""
    def __init__(self):
        super().__init__()
        self.rejections=[]

    def reject(self,event,reason):
        from .notification_decisions import detail
        self.rejections.append(detail(event, reason))


class DependencyReads:
    """At most one in-flight read per priority lane, with no waiting queue."""

    def __init__(self, *, wake=None):
        self._lock = threading.Lock()
        self._lanes = {'ordinary': None, 'holding': None}
        self._wake = wake

    def read(self, lane, reader, deadline, *, key=None, context_key=None):
        if time.monotonic() >= deadline:
            return None
        with self._lock:
            previous = self._lanes[lane]
            if previous is not None and not previous.done():
                return None
            if (previous is not None and key is not None and previous.context_key == context_key
                    and not previous.consumed
                    and time.monotonic() - previous.completed_at <= COMPLETED_RESULT_TTL_SECONDS
                    and time.monotonic() - previous.started_at <= MAX_DEPENDENCY_AGE_SECONDS):
                # Another due receipt must not repeatedly evict this request's
                # completed read before its short retry can consume it.
                if previous.request_key != key:
                    return None
                previous.consumed = True
                try:
                    return previous.result()
                except Exception:
                    return None
            future = Future()
            future.request_key = key
            future.context_key = context_key
            future.started_at = time.monotonic()
            future.consumed = False
            self._lanes[lane] = future

            def run():
                try:
                    result = reader()
                except Exception as exc:
                    future.completed_at = time.monotonic()
                    future.set_exception(exc)
                else:
                    future.completed_at = time.monotonic()
                    future.set_result(result)
                finally:
                    if self._wake is not None:
                        self._wake()

            # A blocked dependency retains its single lane until it finishes.
            # Calls never enqueue work or start replacement threads for it.
            threading.Thread(target=run, name=f'NotificationRead-{lane}', daemon=True).start()
        try:
            result = future.result(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            return None
        if time.monotonic() >= deadline:
            return None
        with self._lock:
            if future.consumed:
                return None
            future.consumed = True
        return result


def holding_lane(events):
    return any(event.get('holding') or event.get('signal_type') in {
        'holding_risk', 'consolidation_divergence'} for event in events)
