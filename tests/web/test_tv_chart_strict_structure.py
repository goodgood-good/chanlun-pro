from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

from chanlun.cl_utils import cl_data_to_tv_chart
from chanlun.cl_utils.strict_chart_runtime import StrictChartRuntimeResult
from tests.trading_system.strict_helpers import strict_evidence_result


def _config() -> dict[str, object]:
    return {
        "chart_show_fx": "0",
        "chart_show_bi": "0",
        "chart_show_xd": "0",
    }


def _frame(evidence=None) -> pd.DataFrame:
    evidence = evidence or strict_evidence_result()
    closed_at = evidence.source_closed_at
    frame = pd.DataFrame(
        (
            {
                "date": closed_at - timedelta(minutes=5),
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1000.0,
            },
            {
                "date": closed_at,
                "open": 10.1,
                "high": 10.4,
                "low": 10.0,
                "close": 10.3,
                "volume": 1200.0,
            },
        )
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision=evidence.price_basis_revision,
    )
    return frame


class _StrictCD:
    def __init__(
        self,
        *,
        evidence=None,
        error: Exception | None = None,
        bis=(),
        xds=(),
    ) -> None:
        self.evidence = evidence or strict_evidence_result()
        self.error = error
        self.evidence_calls = 0
        self.bis = tuple(bis)
        self.xds = tuple(xds)

    def get_idx(self):
        values = np.array((0.0, 0.1))
        return {
            "macd": {
                "dif": values,
                "dea": values,
                "hist": values,
                "hist_area": values,
            }
        }

    def get_bis(self):
        return list(self.bis)

    def get_xds(self):
        return list(self.xds)

    def get_strict_evidence(self):
        self.evidence_calls += 1
        if self.evidence_calls > 1:
            raise AssertionError("strict evidence must be read exactly once")
        if self.error is not None:
            raise self.error
        return self.evidence


def _serialize(cd: _StrictCD, frame: pd.DataFrame | None = None) -> dict:
    evidence = cd.evidence
    return cl_data_to_tv_chart(
        frame if frame is not None else _frame(evidence),
        _config(),
        market="a",
        code=evidence.symbol,
        frequency=evidence.source_frequency,
        strict_runtime=StrictChartRuntimeResult.success(cd),
    )


def test_chart_payload_uses_only_the_strict_snapshot() -> None:
    cd = _StrictCD()

    payload = _serialize(cd)

    assert cd.evidence_calls == 1
    assert payload["strict_structure_mode"] == "replace"
    assert payload["strict_structure"]["schema"] == "chanlun-chart-structure"
    assert payload["strict_structure"]["source_closed_at"] == payload["t"][-1]
    assert len(payload["macd_dif"]) == len(payload["t"]) == 2


def test_segment_payload_distinguishes_forming_formed_and_locked() -> None:
    evidence = strict_evidence_result()
    start_at = evidence.source_closed_at - timedelta(minutes=5)
    end_at = evidence.source_closed_at

    def segment(*, locked: bool, forming: bool):
        start = SimpleNamespace(k=SimpleNamespace(date=start_at), val=10.0)
        end = SimpleNamespace(k=SimpleNamespace(date=end_at), val=11.0)
        return SimpleNamespace(
            start=start,
            end=end,
            forming=forming,
            is_done=lambda: locked,
        )

    cd = _StrictCD(
        evidence=evidence,
        xds=(
            segment(locked=True, forming=False),
            segment(locked=False, forming=False),
            segment(locked=False, forming=True),
        ),
    )
    config = _config() | {"chart_show_xd": "1"}

    payload = cl_data_to_tv_chart(
        _frame(evidence),
        config,
        market="a",
        code=evidence.symbol,
        frequency=evidence.source_frequency,
        strict_runtime=StrictChartRuntimeResult.success(cd),
    )

    assert [item["state"] for item in payload["xds"]] == [
        "locked",
        "formed",
        "forming",
    ]
    # 几何已成形但仍处于防重绘审计缓冲的线段也画实线；只有最后一条
    # forming 线段可使用虚线。
    assert [item["linestyle"] for item in payload["xds"]] == ["0", "0", "1"]
    assert [item["locked"] for item in payload["xds"]] == [True, False, False]


def test_strict_structure_failure_is_atomic_unavailable() -> None:
    cd = _StrictCD(error=ValueError("broken strict evidence"))

    payload = _serialize(cd)

    assert payload["t"]
    assert payload["strict_structure_mode"] == "unavailable"
    assert "strict_structure" not in payload
    assert payload["strict_structure_error"]["code"] == "strict_evidence_invalid"


def test_strict_snapshot_source_close_must_match_display_bars() -> None:
    cd = _StrictCD()
    frame = _frame(cd.evidence)
    frame.loc[frame.index[-1], "date"] -= timedelta(minutes=1)

    payload = _serialize(cd, frame)

    assert payload["strict_structure_mode"] == "unavailable"
    assert payload["strict_structure_error"]["code"] == "strict_context_mismatch"


def test_explicit_metadata_failure_does_not_read_structure() -> None:
    evidence = strict_evidence_result()
    runtime = StrictChartRuntimeResult.unavailable(
        "strict_price_metadata_unavailable",
        "price metadata missing",
    )

    payload = cl_data_to_tv_chart(
        _frame(evidence),
        _config(),
        market="a",
        code=evidence.symbol,
        frequency=evidence.source_frequency,
        strict_runtime=runtime,
    )

    assert payload["strict_structure_mode"] == "unavailable"
    assert payload["strict_structure_error"] == {
        "code": "strict_price_metadata_unavailable"
    }


def test_chart_serializer_drops_future_end_label_before_strict_runtime(
    monkeypatch,
) -> None:
    from cl_app.services import chart_compute

    completed_at = pd.Timestamp("2026-08-05 15:00:00", tz="Asia/Shanghai")
    future_at = pd.Timestamp("2099-01-01 09:30:00", tz="Asia/Shanghai")
    frame = pd.DataFrame(
        (
            {
                "date": completed_at,
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000.0,
            },
            {
                "date": future_at,
                "open": 10.1,
                "high": 10.3,
                "low": 10.0,
                "close": 10.2,
                "volume": 10.0,
            },
        )
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-qmt",
    )
    runtime = object()
    calls: list[tuple[object, ...]] = []

    def build_strict(*, market, code, frequency, frame):
        calls.append(("build", tuple(frame["date"]), dict(frame.attrs)))
        return runtime

    def serialize(frame, config, *, market, code, frequency, strict_runtime):
        calls.append(("serialize", tuple(frame["date"]), strict_runtime))
        return {"strict_structure_mode": "replace"}

    monkeypatch.setattr(chart_compute, "build_strict_chart_cd", build_strict)
    monkeypatch.setattr(chart_compute, "cl_data_to_tv_chart", serialize)

    result = chart_compute.serialize_chart_data_with_strict_runtime(
        market="a",
        code="SH.000001",
        display_frequency="1m",
        display_klines=frame,
        chart_config=_config(),
    )

    assert result == {"strict_structure_mode": "replace"}
    assert calls[0] == (
        "build",
        (completed_at,),
        {
            "structure_price_quantum": "0.01",
            "price_basis_revision": "test-qmt",
        },
    )
    assert calls[1] == ("serialize", (completed_at,), runtime)


def test_supplied_strict_runtime_is_reused(monkeypatch) -> None:
    from cl_app.services import chart_compute

    evidence = strict_evidence_result()
    frame = _frame(evidence).tail(1)
    runtime = object()
    monkeypatch.setattr(
        chart_compute,
        "build_strict_chart_cd",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("supplied strict runtime must be reused")
        ),
    )
    monkeypatch.setattr(
        chart_compute,
        "cl_data_to_tv_chart",
        lambda frame, config, **kwargs: kwargs["strict_runtime"],
    )

    result = chart_compute.serialize_chart_data_with_strict_runtime(
        market="a",
        code=evidence.symbol,
        display_frequency=evidence.source_frequency,
        display_klines=frame,
        chart_config=_config(),
        strict_runtime=runtime,
    )

    assert result is runtime
