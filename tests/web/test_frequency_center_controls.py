from __future__ import annotations

import pandas as pd


def test_center_control_uses_four_fixed_real_periods_for_every_chart() -> None:
    from cl_app.services import chart_compute

    expected = [
        ("1m", "1m 中枢"),
        ("5m", "5m 中枢"),
        ("30m", "30m 中枢"),
        ("d", "日线 中枢"),
    ]
    for frequency in ("1m", "5m", "30m", "d", "15m"):
        assert chart_compute.higher_zs_periods(frequency) == expected


def test_center_control_reuses_current_period_and_fetches_each_higher_period(
    monkeypatch,
) -> None:
    from cl_app.services import chart_compute

    calls: list[str] = []

    def higher(_market, _code, frequency, _config):
        calls.append(frequency)
        return [{"points": [{"time": 1, "price": frequency}]}]

    monkeypatch.setattr(chart_compute, "_higher_zs_for_period", higher)
    current = [{"points": [{"time": 1, "price": "1m"}]}]
    chart_data = {"xd_zss": current}

    assert chart_compute.apply_higher_zs_to_chart_data(
        chart_data,
        "a",
        "SH.600000",
        "1m",
        {},
    ) is True

    assert [item["period"] for item in chart_data["higher_zs"]] == [
        "1m",
        "5m",
        "30m",
        "d",
    ]
    assert all("level" not in item for item in chart_data["higher_zs"])
    assert chart_data["higher_zs"][0]["zss"] == current
    assert calls == ["5m", "30m", "d"]

    # 历史后端显示开关不得绕过新的前端“中枢控制”。
    calls.clear()
    legacy_disabled_data = {"xd_zss": current}
    assert chart_compute.apply_higher_zs_to_chart_data(
        legacy_disabled_data,
        "a",
        "SH.600000",
        "1m",
        {"chart_show_higher_zs": "0"},
    ) is True
    assert [item["period"] for item in legacy_disabled_data["higher_zs"]] == [
        "1m",
        "5m",
        "30m",
        "d",
    ]
    assert calls == ["5m", "30m", "d"]

    calls.clear()
    five_minute_current = [{"points": [{"time": 2, "price": "5m-current"}]}]
    five_minute_data = {"xd_zss": five_minute_current}
    assert chart_compute.apply_higher_zs_to_chart_data(
        five_minute_data,
        "a",
        "SH.600000",
        "5m",
        {},
    ) is True
    assert five_minute_data["higher_zs"][1]["zss"] == five_minute_current
    assert calls == ["1m", "30m", "d"]


def test_one_period_center_reads_displayed_xds_without_recursive_levels(
    monkeypatch,
) -> None:
    from cl_app.services import chart_compute

    class FakeExchange:
        def klines(self, _code, frequency):
            return pd.DataFrame([{"frequency": frequency}])

    class FakeCD:
        def get_xds(self):
            return ["displayed-segment"]

        def get_xd_zss(self):
            raise AssertionError("period center must not read legacy center geometry")

        def get_recursive_branch_levels(self):
            raise AssertionError("period center must not read recursive levels")

    monkeypatch.setattr(chart_compute, "get_exchange", lambda _market: FakeExchange())
    monkeypatch.setattr(
        chart_compute,
        "web_batch_get_cl_datas",
        lambda *_args, **_kwargs: [FakeCD()],
    )
    monkeypatch.setattr(
        chart_compute,
        "xd_segment_centers_to_chart_dicts",
        lambda lines: [{"segments": list(lines), "algorithm": "five-role"}],
    )

    assert chart_compute._higher_zs_for_period(
        "a",
        "SH.600000",
        "5m",
        {},
    ) == [{"segments": ["displayed-segment"], "algorithm": "five-role"}]
