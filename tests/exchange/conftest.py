"""A fresh history admission namespace for each isolated provider scenario."""

import pytest


@pytest.fixture(autouse=True)
def isolated_longbridge_history_queue(monkeypatch, tmp_path):
    from chanlun.exchange import longbridge_history_limit

    monkeypatch.setattr(
        longbridge_history_limit,
        "_default_root",
        lambda: tmp_path / "longbridge_history",
    )
