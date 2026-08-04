from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

from chanlun.cl_utils import cl_data_to_tv_chart
from tests.trading_system.strict_helpers import strict_evidence_result


def _config() -> dict[str, object]:
    return {
        "chart_show_fx": "0",
        "chart_show_bi": "0",
        "chart_show_xd": "0",
        "chart_show_bi_zs": "1",
        "chart_show_xd_zs": "1",
        "chart_show_recursive_levels": "1",
        "chart_use_branch_core": "1",
        "chart_show_xd_zslx": "1",
        "chart_show_bi_bc": "1",
        "chart_show_xd_bc": "1",
        "chart_show_bi_mmd": "1",
        "chart_show_xd_mmd": "1",
        "zs_bi_type": ["bz"],
        "zs_xd_type": ["bz"],
        "idx_macd_fast": 12,
        "idx_macd_slow": 26,
        "idx_macd_signal": 9,
    }


class _StrictOnlyCD:
    def __init__(self, *, evidence=None, error: Exception | None = None) -> None:
        self.evidence = evidence or strict_evidence_result()
        self.error = error
        self.evidence_calls = 0
        self.base_center_calls = {"bi": 0, "xd": 0}
        closed_at = self.evidence.source_closed_at
        self._bars = [
            SimpleNamespace(
                date=closed_at - timedelta(minutes=5),
                h=10.2,
                l=9.8,
                o=10.0,
                c=10.1,
                a=1000.0,
            ),
            SimpleNamespace(
                date=closed_at,
                h=10.4,
                l=10.0,
                o=10.1,
                c=10.3,
                a=1200.0,
            ),
        ]

    def get_code(self):
        return self.evidence.symbol

    def get_frequency(self):
        return self.evidence.source_frequency

    def get_klines(self):
        return list(self._bars)

    def get_src_klines(self):
        return list(self._bars)

    def get_bis(self):
        return []

    def get_xds(self):
        return []

    def get_bi_zss(self):
        self.base_center_calls["bi"] += 1
        return []

    def get_xd_zss(self):
        self.base_center_calls["xd"] += 1
        return []

    def get_idx(self):
        values = np.array([0.0, 0.1])
        return {
            "macd": {
                "dif": values,
                "dea": values,
                "hist": values,
                "hist_area": values,
            }
        }

    def get_strict_evidence(self):
        self.evidence_calls += 1
        if self.evidence_calls > 1:
            raise AssertionError("strict evidence must be read exactly once")
        if self.error is not None:
            raise self.error
        return self.evidence

    def _legacy(self, *_args, **_kwargs):
        raise AssertionError("legacy chart structure source must not be read")

    get_bi_zhongshu = _legacy
    get_xd_zslx = _legacy
    get_recursive_branch_levels = _legacy
    get_kuozhan_levels = _legacy
    get_branch_bspoints = _legacy
    get_branch_bcs = _legacy


def test_chart_payload_keeps_base_centers_separate_from_strict_sources() -> None:
    cd = _StrictOnlyCD()

    payload = cl_data_to_tv_chart(cd, _config())

    assert cd.evidence_calls == 1
    assert cd.base_center_calls == {"bi": 1, "xd": 0}
    assert payload["strict_structure_mode"] == "replace"
    assert payload["strict_structure"]["schema"] == "chanlun-chart-structure/v5"
    assert payload["strict_structure"]["source_closed_at"] == payload["t"][-1]
    assert payload["bi_zss"] == []
    assert payload["xd_zss"] == []
    for legacy_field in (
        "bcs",
        "mmds",
        "recursive_levels",
        "interval_nest",
    ):
        assert legacy_field not in payload


def test_strict_structure_failure_is_atomic_unavailable_not_legacy_fallback() -> None:
    cd = _StrictOnlyCD(error=ValueError("broken strict evidence"))

    payload = cl_data_to_tv_chart(cd, _config())

    assert payload["t"]
    assert payload["strict_structure_mode"] == "unavailable"
    assert "strict_structure" not in payload
    assert payload["strict_structure_error"]["code"] == "strict_evidence_invalid"
    assert cd.evidence_calls == 1


def test_strict_snapshot_source_close_must_match_display_bars() -> None:
    cd = _StrictOnlyCD()
    cd._bars[-1].date = cd._bars[-1].date - timedelta(minutes=1)

    payload = cl_data_to_tv_chart(cd, _config())

    assert payload["strict_structure_mode"] == "unavailable"
    assert payload["strict_structure_error"]["code"] == "strict_context_mismatch"
    assert "strict_structure" not in payload


def test_low_to_high_display_recomputes_strict_structure_on_display_bars(
    monkeypatch,
) -> None:
    from cl_app.services import chart_compute

    source = pd.DataFrame(
        [
            {
                "date": strict_evidence_result().source_closed_at,
                "code": "SH.600519",
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 1000.0,
            }
        ]
    )
    source.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw-v1",
    )
    converted = source.copy(deep=True)
    calls = []
    sentinel_cd = object()

    def convert(market, frame, target):
        calls.append(("convert", market, tuple(frame["date"]), target))
        return converted

    def build(market, code, frames, config):
        calls.append(("build", market, code, tuple(frames), config))
        assert frames["30m"].attrs == source.attrs
        return [sentinel_cd]

    monkeypatch.setattr(
        chart_compute,
        "_convert_chart_frequency",
        convert,
    )
    monkeypatch.setattr(chart_compute, "web_batch_get_cl_datas", build)

    cd, display = chart_compute._build_display_frequency_cl(
        market="a",
        code="SH.600519",
        fetched_klines=source,
        fetched_frequency="5m",
        display_frequency="30m",
        cl_config={"strict": True},
    )

    assert cd is sentinel_cd
    assert display is not source
    assert tuple(display["date"]) == tuple(converted["date"])
    assert calls[0][0:2] == ("convert", "a")
    assert calls[0][3] == "30m"
    assert calls[1][0:4] == ("build", "a", "SH.600519", ("30m",))


def test_chart_payload_reads_explicit_strict_runtime_not_legacy_cd() -> None:
    from chanlun.cl_utils.strict_chart_runtime import StrictChartRuntimeResult

    legacy = _StrictOnlyCD()
    strict = _StrictOnlyCD()
    legacy.get_strict_evidence = lambda: (_ for _ in ()).throw(
        AssertionError("legacy CL must not provide strict evidence")
    )

    payload = cl_data_to_tv_chart(
        legacy,
        _config(),
        strict_runtime=StrictChartRuntimeResult.success(strict),
    )

    assert strict.evidence_calls == 1
    assert payload["strict_structure_mode"] == "replace"


def test_explicit_metadata_failure_does_not_call_any_strict_source() -> None:
    from chanlun.cl_utils.strict_chart_runtime import StrictChartRuntimeResult

    legacy = _StrictOnlyCD()
    legacy.get_strict_evidence = lambda: (_ for _ in ()).throw(
        AssertionError("strict evidence must not run")
    )
    runtime = StrictChartRuntimeResult.unavailable(
        "strict_price_metadata_unavailable",
        "price metadata missing",
    )

    payload = cl_data_to_tv_chart(legacy, _config(), strict_runtime=runtime)

    assert payload["t"]
    assert payload["strict_structure_mode"] == "unavailable"
    assert payload["strict_structure_error"] == {
        "code": "strict_price_metadata_unavailable"
    }
