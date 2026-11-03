"""Bounded, best-effort US source rejection logs for owned compute children."""

from functools import wraps
import logging
import re

from chanlun.exchange.errors import SourcePreparationPending


_LOGGER = logging.getLogger(__name__)
_MARKER = "_us_source_rejection_diagnostics_v1"
_STATIC_REASONS = {
    "Source did not provide a verified complete price basis": "incomplete_price_basis",
    "Source history clock is invalid": "invalid_history_clock",
    "Source window is empty": "empty_window",
    "Source frame values or identity are invalid": "invalid_values_or_identity",
    "Source did not reach the completed market clock": "completed_clock_not_reached",
}


def _reason(error, original):
    # A provider may raise the same text. Only the original reader's own
    # terminal raise site is evidence of one of its static validation rejects.
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    if (type(error) is SourcePreparationPending
            and error.code == "us_completed_clock_pending"
            and trace is not None and trace.tb_frame.f_code is original.__code__):
        return "completed_clock_not_reached"
    if (
        type(error) is ValueError
        and len(error.args) == 1
        and type(error.args[0]) is str
        and trace is not None
        and trace.tb_frame.f_code is original.__code__
    ):
        return _STATIC_REASONS.get(error.args[0], "unclassified_source_failure")
    return "unclassified_source_failure"


def _safe_code(value):
    return (
        value
        if type(value) is str and re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}\.US", value)
        else "unknown"
    )


def _safe_frequency(value):
    return (
        value
        if type(value) is str and re.fullmatch(r"(?:[1-9][0-9]{0,3}m|[dwmqy])", value)
        else "unknown"
    )


def _wrap_read(original):
    @wraps(original)
    def read(self, name, code, frequency, start_date, end_date, args):
        try:
            return original(self, name, code, frequency, start_date, end_date, args)
        except Exception as error:
            try:
                provider = name if type(name) is str and name in {"usmart", "longbridge"} else "unknown"
                # Never attach the exception, traceback, request arguments,
                # dates, SDK text, or arbitrary SDK error codes to the record.
                _LOGGER.warning(
                    "us_source_rejection provider=%s code=%s frequency=%s reason=%s",
                    provider, _safe_code(code), _safe_frequency(frequency),
                    _reason(error, original),
                )
            except BaseException:
                # Diagnostics cannot replace the original failure, including
                # cancellation, even if a logging handler itself fails.
                pass
            raise

    setattr(read, _MARKER, True)
    return read


def install_source_rejection_diagnostics():
    try:
        from chanlun.exchange.us_dual_history import DualUSHistory

        if not getattr(DualUSHistory._read, _MARKER, False):
            DualUSHistory._read = _wrap_read(DualUSHistory._read)
        return True
    except Exception:
        # A diagnostic import/install failure must not disable the worker.
        return False


def diagnostic_reusable_worker(connection):
    # This importable spawn target runs in each owned compute child. Installing
    # only in the coordinator's main() would not survive Windows spawn.
    install_source_rejection_diagnostics()
    from chanlun.screening.worker_pool import reusable_worker

    return reusable_worker(connection)
