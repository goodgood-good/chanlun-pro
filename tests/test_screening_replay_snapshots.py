import pandas as pd
import pytest

from chanlun.screening import replay_snapshots as replays


def frame():
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-10-12", periods=3, tz="Asia/Shanghai"),
            "open": [1.0, 2.0, 3.0],
            "close": [2.0, 3.0, 4.0],
            "volume": [10.0, 20.0, 30.0],
        }
    )


def test_only_independent_replays_share_full_or_prefix_inputs_and_return_owned_values():
    calls = []

    def builder(data, code, frequency, **kwargs):
        calls.append((len(data), kwargs))
        return {"levels": [{"points": [{"price": float(data.close.iloc[-1])}]}]}

    data = frame()
    with replays.replay_scope():
        first = replays.replay_snapshot(builder, data, "A", "5m")
        first["levels"][0]["points"][0]["price"] = 999
        with replays.replay_scope():
            again = replays.replay_snapshot(builder, data.copy(), "A", "5m")
        assert again["levels"][0]["points"][0]["price"] == 4
        replays.replay_snapshot(builder, data.iloc[:-1], "A", "5m")
        replays.replay_snapshot(builder, data.iloc[:-1].copy(), "A", "5m")
        replays.replay_snapshot(builder, data, "A", "5m", include_consolidation=True)
    assert len(calls) == 3
    replays.replay_snapshot(builder, data, "A", "5m")
    assert len(calls) == 4, "No proof or state survives a screening call"


@pytest.mark.parametrize(
    "change",
    ["volume", "price", "time", "metadata", "index", "code", "period", "builder"],
)
def test_any_input_or_contract_difference_rebuilds(change):
    calls = []

    def builder(*args, **kwargs):
        calls.append(True)
        return {"verified": len(calls)}

    data = frame()
    code = "A"
    period = "5m"
    second = builder
    with replays.replay_scope():
        replays.replay_snapshot(builder, data, code, period)
        data = data.copy(deep=True)
        if change == "volume":
            data.loc[0, "volume"] += 1
        elif change == "price":
            data.loc[0, "open"] += 0.1
        elif change == "time":
            data.loc[0, "date"] += pd.Timedelta(minutes=1)
        elif change == "metadata":
            data.attrs["price_basis_revision"] = "changed"
        elif change == "index":
            data.index = [1, 2, 3]
        elif change == "code":
            code = "B"
        elif change == "period":
            period = "1m"
        else:
            second = lambda *a, **k: builder(*a, **k)
        replays.replay_snapshot(second, data, code, period)
    assert len(calls) == 2


def test_failed_replay_is_not_cached_and_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(replays, "MAX_ENTRIES", 1)
    count = [0]

    def builder(*args, **kwargs):
        count[0] += 1
        if count[0] == 1:
            raise ValueError("incomplete rebuild")
        return {"ok": True}

    data = frame()
    with replays.replay_scope():
        with pytest.raises(ValueError):
            replays.replay_snapshot(builder, data, "A", "5m")
        replays.replay_snapshot(builder, data, "A", "5m")
        replays.replay_snapshot(builder, data.iloc[:2], "A", "5m")
        replays.replay_snapshot(builder, data, "A", "5m")
    assert count[0] == 4
