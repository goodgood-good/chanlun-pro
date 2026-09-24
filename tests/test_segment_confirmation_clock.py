"""Regressions from the frozen 2026-09-19 real-market problem cases."""

from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.cl_utils.tv_chart import cl_data_to_tv_chart
from chanlun.screening.runner import snapshot_for


FIXTURES = Path(__file__).parent / "fixtures/segment_clock"


def frame(code, frequency):
    return pd.read_parquet(FIXTURES / f"{code}_{frequency}.parquet")


def runtime(data, code, frequency, *, closed):
    result = build_strict_chart_cd(
        market="us" if code == "QQQ.US" else "a", code=code,
        frequency=frequency, frame=data, last_bar_closed=closed,
    )
    assert result.cd is not None, result.error_message
    return result


def epoch(value):
    return int(pd.Timestamp(value, tz="Asia/Shanghai").timestamp())


def matching_segment(snapshot, start, end):
    return next(s for s in snapshot["screening_segments"]
                if s["start_time"] == epoch(start) and s["end_time"] == epoch(end))


def test_last_closed_minute_confirms_a08_without_a_future_candle():
    data = frame("SZ.000766", "1m")
    end = pd.Timestamp("2026-09-18 13:52", tz="Asia/Shanghai")
    def segment(cd):
        return next(s for s in cd.get_xds() if s.end.k.date == end)

    before = runtime(data.iloc[:-1], "SZ.000766", "1m", closed=True).cd
    intrabar = runtime(data, "SZ.000766", "1m", closed=False).cd
    assert not segment(before).done and not segment(intrabar).done
    snapshot = snapshot_for(data, "SZ.000766", "1m")
    fixed = matching_segment(snapshot, "2026-09-18 09:37", "2026-09-18 13:52")
    assert fixed["locked"] and not fixed["forming"]
    assert fixed["confirmed_at"] == epoch("2026-09-18 15:00") == int(data.date.iloc[-1].timestamp())


@pytest.mark.parametrize("code,start,end,confirmed", [
    ("BJ.920268", "2026-09-09 14:30", "2026-09-11 13:15", "2026-09-15 13:40"),
    ("SZ.301697", "2026-09-01 10:25", "2026-09-14 09:35", "2026-09-16 11:25"),
    ("SZ.301699", "2026-09-09 11:05", "2026-09-14 09:35", "2026-09-16 10:45"),
    ("SZ.301699", "2026-09-14 09:35", "2026-09-16 09:35", "2026-09-17 13:25"),
])
def test_short_history_saves_segment_clocks_without_a_recursive_level(code, start, end, confirmed):
    data = frame(code, "5m")
    cd = runtime(data, code, "5m", closed=True).cd
    assert not cd.get_strict_evidence().structure.levels
    snapshot = snapshot_for(data, code, "5m")
    segment = matching_segment(snapshot, start, end)
    assert segment["locked"] and segment["confirmed_at"] == epoch(confirmed)
    assert len(snapshot["screening_segments"]) == len(cd.get_xds())


def test_holiday_does_not_delay_c01_to_the_next_session():
    data = frame("SH.600658", "5m")
    snapshot = snapshot_for(data, "SH.600658", "5m")
    segment = matching_segment(snapshot, "2026-02-02 15:00", "2026-02-09 13:30")
    assert segment["confirmed_at"] == epoch("2026-02-13 15:00")
    prefix = data.loc[data.date <= pd.Timestamp("2026-02-13 15:00", tz="Asia/Shanghai")]
    same = matching_segment(snapshot_for(prefix, "SH.600658", "5m"), "2026-02-02 15:00", "2026-02-09 13:30")
    assert same["confirmed_at"] == segment["confirmed_at"]


def test_qqq_shared_confirmation_is_available_on_its_actual_closed_minute():
    snapshot = snapshot_for(frame("QQQ.US", "1m"), "QQQ.US", "1m")
    for start, end in [("2026-09-02 02:47", "2026-09-02 22:36"),
                       ("2026-09-02 22:36", "2026-09-03 01:20")]:
        assert matching_segment(snapshot, start, end)["confirmed_at"] == epoch("2026-09-03 02:08")


def test_start_labels_keep_chart_coordinates_but_use_actual_close_times():
    ended = frame("QQQ.US", "1m")
    started = ended.copy()
    started["date"] -= pd.Timedelta(minutes=1)
    started.attrs["bar_time_label"] = "start"
    expected = runtime(ended, "QQQ.US", "1m", closed=True).cd
    result = runtime(started, "QQQ.US", "1m", closed=True)
    assert [s.locked_at for s in result.cd.get_xds()] == [s.locked_at for s in expected.get_xds()]
    assert result.cd._strict_as_of() == ended.date.iloc[-1]
    assert [s.end.k.date + pd.Timedelta(minutes=1) for s in result.cd.get_xds()] == [s.end.k.date for s in expected.get_xds()]
    chart = cl_data_to_tv_chart(started, {}, market="us", code="QQQ.US", frequency="1m", strict_runtime=result)
    assert chart["t"] == [int(t.timestamp()) for t in started.date]
    assert chart["strict_structure_mode"] == "replace"
    assert chart["strict_structure"]["source_closed_at"] == int(ended.date.iloc[-1].timestamp())
