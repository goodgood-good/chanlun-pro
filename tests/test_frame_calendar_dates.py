"""Date validation scales by trading days and preserves local-clock boundaries."""
import pandas as pd
import pytest

from chanlun.screening.rules import validate_frame


def bars(frequency="5m", *, timezone="Asia/Shanghai", days=None):
    days = days or pd.bdate_range("2026-09-01", periods=30).strftime("%Y-%m-%d").tolist()
    step = int(frequency[:-1])
    stamps = [pd.Timestamp(day + " 00:00") + pd.Timedelta(minutes=base + offset)
              for day in days for base in (570, 780) for offset in range(step, 121, step)]
    dates = pd.DatetimeIndex(stamps).tz_localize(timezone)
    frame = pd.DataFrame({"date": dates, "open": 10., "high": 11., "low": 9., "close": 10., "volume": 100.},
                         index=[f"bar-{i}" for i in range(len(dates))])
    frame.attrs["source_identity"] = "preserved"
    context = {"frequency": frequency, "session_timezone": timezone, "trading_days": days,
               "calendar_coverage_start": days[0], "cutoff": int(dates[-1].timestamp())}
    return frame, context


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
@pytest.mark.parametrize("source_timezone", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_calendar_date_formatting_is_bounded_by_unique_days(frequency, source_timezone, monkeypatch):
    frame, context = bars(frequency)
    frame["date"] = frame.date.dt.tz_convert(source_timezone)
    original = frame.copy(deep=True)
    formatter = pd.DatetimeIndex.strftime
    formatted = []
    def format_dates(values, *args, **kwargs):
        formatted.append(len(values))
        return formatter(values, *args, **kwargs)
    monkeypatch.setattr(pd.DatetimeIndex, "strftime", format_dates)
    assert validate_frame(frame, context) == []
    assert sum(formatted) <= len(context["trading_days"])
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original.attrs


@pytest.mark.parametrize("change,expected", [
    ("missing_day", ["INVALID_SESSION"]),
    ("before_coverage", []),
    ("zero_volume", ["NO_VOLUME"]),
    ("stale", ["STALE_DATA"]),
    ("subsecond", ["INVALID_SESSION"]),
    ("out_of_session", ["INVALID_SESSION"]),
])
def test_calendar_and_close_diagnostics_are_preserved(change, expected):
    frame, context = bars()
    if change == "missing_day":
        context["trading_days"].pop(5)
    elif change == "before_coverage":
        context["calendar_coverage_start"] = context["trading_days"][5]
        context["trading_days"] = context["trading_days"][5:]
    elif change == "zero_volume":
        frame.loc[frame.index[-48:], "volume"] = 0
    elif change == "stale":
        context["cutoff"] += 300
    elif change == "subsecond":
        frame.loc[frame.index[10], "date"] += pd.Timedelta(nanoseconds=1)
    else:
        frame.loc[frame.index[0], "date"] -= pd.Timedelta(minutes=5)
    assert validate_frame(frame, context) == expected


@pytest.mark.parametrize("day", ["2026-03-08", "2026-11-01"])
def test_day_checks_do_not_localize_an_ambiguous_or_missing_dst_midnight(day):
    days = pd.date_range(pd.Timestamp(day) - pd.Timedelta(days=1), periods=3).strftime("%Y-%m-%d").tolist()
    frame, context = bars("1m", timezone="America/Havana", days=days)
    assert validate_frame(frame, context) == []


def test_qmt_auction_rows_keep_their_existing_exception():
    frame, context = bars("1m")
    extra = frame.iloc[[0]].copy()
    extra["date"] = pd.to_datetime([context["trading_days"][0] + " 09:25"]).tz_localize("Asia/Shanghai")
    extra.index = ["auction"]
    frame = pd.concat([extra, frame])
    assert validate_frame(frame, context) == ["INVALID_SESSION"]
    frame.attrs["price_basis_provider"] = "qmt"
    assert validate_frame(frame, context) == []
