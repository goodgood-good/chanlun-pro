from __future__ import annotations

from decimal import Decimal

import pandas as pd

from chanlun.cl_utils import strict_chart_runtime
from chanlun.decision_support.trading_system.runtime_config import strict_cl_config


def _frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime([1736402400, 1736402460], unit="s", utc=True),
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": [100.0, 120.0],
        }
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="sha256:test-basis",
        price_basis_provider="qmt",
        price_basis_adjustment="front",
    )
    return frame


def test_strict_chart_runtime_uses_only_fixed_strict_config(monkeypatch) -> None:
    sentinel = object()
    captured = {}

    def build(market, code, frames, config):
        captured.update(
            market=market,
            code=code,
            frequency=tuple(frames),
            frame=frames["1m"],
            config=config,
        )
        return [sentinel]

    monkeypatch.setattr(strict_chart_runtime, "web_batch_get_cl_datas", build)
    frame = _frame()

    result = strict_chart_runtime.build_strict_chart_cd(
        market="a", code="SH.600926", frequency="1m", frame=frame
    )

    assert result.cd is sentinel
    assert result.error_code is None
    assert captured["frame"] is frame
    assert captured["config"] == strict_cl_config(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="sha256:test-basis",
    )
    assert "chart_show_fx" not in captured["config"]


def test_strict_chart_runtime_fails_closed_before_cl_build(monkeypatch) -> None:
    monkeypatch.setattr(
        strict_chart_runtime,
        "web_batch_get_cl_datas",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CL builder must not run")
        ),
    )

    result = strict_chart_runtime.build_strict_chart_cd(
        market="a",
        code="SH.600926",
        frequency="1m",
        frame=pd.DataFrame({"close": [10.0]}),
    )

    assert result.cd is None
    assert result.error_code == "strict_price_metadata_unavailable"
    assert "metadata" in result.error_message
