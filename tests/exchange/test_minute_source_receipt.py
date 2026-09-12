"""A shortcut may reuse only the exact, fresh, complete provider response."""

import pandas as pd
import pytest

from chanlun.exchange import minute_source_receipt as receipt


@pytest.fixture
def sealed(monkeypatch):
    monkeypatch.setattr(receipt.time, "monotonic", lambda: 100.0)
    frame = pd.DataFrame({
        "date": pd.date_range("2026-09-04 19:56", periods=5, freq="min", tz="UTC"),
        "code": ["AAPL.US"] * 5,
        "open": [10.0] * 5, "high": [11.0] * 5, "low": [9.0] * 5,
        "close": [10.5] * 5, "volume": [100.0] * 5,
    })
    frame.attrs.update(
        bar_time_label="end", canonical_minute_cutoff=frame.iloc[-1].date.isoformat(),
        structure_price_quantum="0.01", price_basis_revision="raw-test",
        price_basis_provider="test", price_basis_adjustment="none",
    )
    return receipt.seal_minute_source(frame, owner="provider-1", code="AAPL.US", args={"right": 0})


def matches(frame, **kwargs):
    return receipt.matches_minute_source(frame, **{
        "owner": "provider-1", "code": "AAPL.US", "args": {"right": 0}, **kwargs,
    })


def test_exact_copied_response_can_be_reused_but_not_by_other_providers_or_queries(sealed):
    assert matches(sealed.copy(deep=True))
    assert not matches(sealed, owner="provider-2")
    assert not matches(sealed, code="TSLA.US")
    assert not matches(sealed, args={"right": 1})


@pytest.mark.parametrize("change", [
    "head", "tail", "middle_price", "volume", "code", "date", "quantum",
    "basis", "provider", "adjustment", "label", "cutoff", "incomplete", "receipt",
])
def test_mutated_or_shortened_history_is_never_attested(sealed, change):
    frame = sealed.copy(deep=True)
    if change == "head":
        frame = frame.iloc[1:]
    elif change == "tail":
        frame = frame.iloc[:-1]
    elif change == "middle_price":
        frame.loc[1, "high"] += 1
    elif change == "volume":
        frame.loc[1, "volume"] += 1
    elif change == "code":
        frame.loc[1, "code"] = "TSLA.US"
    elif change == "date":
        frame.loc[1, "date"] += pd.Timedelta(seconds=1)
    else:
        key, value = {
            "quantum": ("structure_price_quantum", "0.001"),
            "basis": ("price_basis_revision", "other"),
            "provider": ("price_basis_provider", "other"),
            "adjustment": ("price_basis_adjustment", "forward"),
            "label": ("bar_time_label", "start"),
            "cutoff": ("canonical_minute_cutoff", "2026-09-04T20:01:00Z"),
            "incomplete": ("fetch_incomplete", True),
            "receipt": ("_closed_minute_receipt", {}),
        }[change]
        frame.attrs[key] = value
    assert not matches(frame)


@pytest.mark.parametrize("age, valid", [(0, True), (29.99, True), (30, False), (31, False), (-1, False)])
def test_receipt_has_a_short_monotonic_lifetime(sealed, monkeypatch, age, valid):
    monkeypatch.setattr(receipt.time, "monotonic", lambda: 100.0 + age)
    assert matches(sealed) is valid


@pytest.mark.parametrize("problem", ["no_cutoff", "wrong_cutoff", "start_label", "incomplete", "empty"])
def test_ordinary_or_incomplete_frame_cannot_be_sealed(sealed, problem):
    frame = sealed.copy(deep=True)
    frame.attrs.pop("_closed_minute_receipt")
    if problem == "no_cutoff":
        frame.attrs.pop("canonical_minute_cutoff")
    elif problem == "wrong_cutoff":
        frame.attrs["canonical_minute_cutoff"] = "2026-09-04T20:01:00Z"
    elif problem == "start_label":
        frame.attrs["bar_time_label"] = "start"
    elif problem == "incomplete":
        frame.attrs["fetch_incomplete"] = True
    else:
        frame = frame.iloc[:0]
    receipt.seal_minute_source(frame, owner="provider-1", code="AAPL.US", args={"right": 0})
    assert "_closed_minute_receipt" not in frame.attrs
