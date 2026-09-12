from __future__ import annotations

from pathlib import Path

import pandas as pd

from chanlun.core.strict_structure.base_profile import strict_base_config


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def strict_config():
    return {
        **strict_base_config(),
        "structure_price_quantum": "0.01",
        "price_basis_revision": "test-raw",
        "strict_config_revision": "sha256:test-strict-runtime",
    }


def load_frame(name, rows):
    return (
        pd.read_parquet(FIXTURES / name)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(rows)
        .reset_index(drop=True)
    )
