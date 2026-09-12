from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd

from chanlun.cl_utils import cl_data_to_tv_chart
from chanlun.cl_utils.strict_chart_runtime import StrictChartRuntimeResult
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import SourceKind


class _StrictChartCD:
    def __init__(self) -> None:
        self.evidence = SimpleNamespace(source_closed_at=datetime(2026,8,5,7,0,tzinfo=timezone.utc), price_basis_revision="test-raw", symbol="SH.600088", source_frequency="5m")
        self._strict_htf_macd_by_level = {
            0: {
                "dif": [10.0, -5.0, 2.0, 100.0, -100.0, 8.0, 50.0, 4.0],
                "dea": [9.0, -4.0, 1.0, 90.0, -90.0, 4.0, 40.0, 2.0],
                "hist": [2.0, -2.0, 2.0, 20.0, -20.0, 8.0, 20.0, 4.0],
                "bucket_keys": [0, 0, 0, 1, 1, 1, 2, 2],
                "algorithm": "causal-partial-htf",
            }
        }

    def get_idx(self):
        values = np.zeros(8)
        return {
            "macd": {
                "dif": values,
                "dea": values,
                "hist": values,
                "hist_area": values,
            }
        }

    def get_bis(self):
        return []

    def get_xds(self):
        return []

    def get_native_centers(self):
        return calculate_centers((), 0, SourceKind.SEGMENT)

    def get_code(self):
        return self.evidence.symbol

    def get_frequency(self):
        return self.evidence.source_frequency

    def _strict_as_of(self):
        return self.evidence.source_closed_at

    def _strict_price_quantum(self):
        return Decimal("0.01")

    def _strict_price_basis_revision(self):
        return self.evidence.price_basis_revision

    def _strict_config_revision(self):
        return "test-native-v2"


def test_tv_payload_smooths_htf_without_mutating_runtime_macd() -> None:
    cd = _StrictChartCD()
    strict_dif = list(cd._strict_htf_macd_by_level[0]["dif"])
    closed_at = cd.evidence.source_closed_at
    frame = pd.DataFrame(
        [
            {
                "date": closed_at - timedelta(minutes=5 * (7 - index)),
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0 + index / 10,
                "volume": 1000.0,
            }
            for index in range(8)
        ]
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision=cd.evidence.price_basis_revision,
    )

    payload = cl_data_to_tv_chart(
        frame,
        {"chart_show_fx": "0", "chart_show_bi": "0", "chart_show_xd": "0"},
        market="a",
        code=cd.evidence.symbol,
        frequency=cd.evidence.source_frequency,
        strict_runtime=StrictChartRuntimeResult.success(cd),
    )

    assert payload is not None
    assert payload["strict_structure_mode"] == "replace"
    assert payload["higher_macd_dif"] == [2.0, 2.0, 2.0, 4.0, 6.0, 8.0, 6.0, 4.0]
    assert cd._strict_htf_macd_by_level[0]["dif"] == strict_dif
