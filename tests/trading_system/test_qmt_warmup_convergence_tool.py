from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupPrefixObservation,
    classify_warmup_convergence_envelope,
)
from chanlun.decision_support.trading_system.candidate_warmup_diagnostics import (
    candidate_warmup_parameter_document,
    candidate_warmup_presentation,
    select_candidate_warmup_rows,
    validate_candidate_warmup_diagnostic_document,
)
from tools.audit_qmt_warmup_convergence import (
    _snapshot_codes,
    collect_qmt_warmup_convergence,
    qmt_local_frame_provider,
)


AS_OF = datetime.fromisoformat("2026-07-29T14:30:00+08:00")


def _frame(rows: int, *, frequency: str = "1min") -> pd.DataFrame:
    result = pd.DataFrame(
        {
            "date": pd.date_range(
                end=AS_OF,
                periods=rows,
                freq=frequency,
            ),
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 1000.0,
        }
    )
    result.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-front-v1",
    )
    return result


def _stable_envelope(*, frequency: str, as_of: datetime):
    observations = tuple(
        WarmupPrefixObservation(
            bar_count=count,
            starts_at=as_of - timedelta(minutes=count),
            signature_sha256="sha256:" + "a" * 64,
        )
        for count in (100, 200, 300)
    )
    return classify_warmup_convergence_envelope(
        frequency=frequency,
        as_of=as_of,
        parameter_set_id="sha256:" + "b" * 64,
        observations=observations,
    )


def test_collect_is_diagnostic_only_and_never_changes_active_gate() -> None:
    calls = []

    def frames(**kwargs):
        calls.append(kwargs)
        return _frame(4)

    def auditor(**kwargs):
        return _stable_envelope(
            frequency=kwargs["frequency"],
            as_of=kwargs["as_of"],
        )

    report = collect_qmt_warmup_convergence(
        codes=("SH.600000",),
        frequencies=("30m", "1m"),
        as_of=AS_OF,
        snapshot_content_sha256="sha256:" + "c" * 64,
        frame_provider=frames,
        auditor=auditor,
    )

    assert [call["bar_budget"] for call in calls] == [2400, 14400]
    assert report["status"] == "COMPLETE"
    assert report["classification_counts"] == [
        {"status": "STABLE_ALL_PREFIXES", "count": 2}
    ]
    assert report["diagnostic_only"] is True
    assert report["active_gate_unchanged"] is True
    assert report["ranking_parameters_unchanged"] is True
    assert report["portfolio_performance_evaluable"] is False
    assert report["real_account_accessed"] is False
    assert report["real_order_transport_enabled"] is False
    assert report["completed_bars_only"] is True
    assert report["qmt_skip_download"] is True
    assert report["minimum_market_data_frequency"] == "1m"
    assert report["tick_data_used"] is False
    assert report["live_status"] == "LIVE_DISABLED"
    assert str(report["content_sha256"]).startswith("sha256:")
    validate_candidate_warmup_diagnostic_document(report)


def test_qmt_local_frame_provider_sets_skip_download() -> None:
    class Exchange:
        def __init__(self) -> None:
            self.calls = []

        def klines(self, code, frequency, *, args):
            self.calls.append((code, frequency, args))
            return _frame(500, frequency="1D")

    exchange = Exchange()
    provider = qmt_local_frame_provider(exchange)

    result = provider(
        code="SH.600000",
        frequency="d",
        as_of=AS_OF,
        bar_budget=1600,
    )

    assert len(result) == 500
    assert exchange.calls == [
        (
            "SH.600000",
            "d",
            {
                "req_counts": 1600,
                "skip_download": True,
                "dividend_type": "front",
            },
        )
    ]


def test_snapshot_signal_selection_is_ordered_and_unique() -> None:
    snapshot = {
        "signals": (
            {"code": "SH.600000"},
            {"code": "SH.600000"},
            {"code": "SZ.000001"},
            {"code": "not-a-stock"},
        )
    }

    assert _snapshot_codes(snapshot, explicit=None, limit=2) == (
        "SH.600000",
        "SZ.000001",
    )


def test_modern_snapshot_selection_uses_review_facts_and_deduplicates() -> None:
    snapshot = {
        "signals": [
            {
                "code": "SH.600003",
                "side": "buy",
                "point_type": "3buy",
                "lifecycle_stage": "approaching",
                "sector": {"horizontal_rank": 2},
            },
            {
                "code": "SH.600002",
                "side": "buy",
                "point_type": "1buy",
                "lifecycle_stage": "triggered",
                "sector": {"horizontal_rank": 8},
            },
            {
                "code": "SH.600001",
                "side": "sell",
                "point_type": "1sell",
                "lifecycle_stage": "triggered",
                "sector": {"horizontal_rank": 1},
            },
            {
                "code": "SH.600003",
                "side": "buy",
                "point_type": "1buy",
                "lifecycle_stage": "observed",
                "sector": {"horizontal_rank": 1},
            },
            {
                "code": "SH.600004",
                "side": "buy",
                "point_type": "2buy",
                "lifecycle_stage": "armed",
                "sector": {"horizontal_rank": 1},
            },
        ]
    }

    selected = select_candidate_warmup_rows(snapshot, limit=3)

    assert [row["code"] for row in selected] == [
        "SH.600002",
        "SH.600004",
        "SH.600003",
    ]
    assert [row["rank"] for row in selected] == [1, 2, 3]
    assert all(row["selection_profile"] == "MODERN_BUY_REVIEW_ORDER" for row in selected)


def test_compact_presentation_is_bound_and_does_not_claim_a_gate() -> None:
    def frames(**_kwargs):
        return _frame(4)

    def auditor(**kwargs):
        return _stable_envelope(
            frequency=kwargs["frequency"],
            as_of=kwargs["as_of"],
        )

    source = "sha256:" + "d" * 64
    parameters = candidate_warmup_parameter_document(
        candidate_limit=1,
        frequencies=("d", "1m"),
    )
    report = collect_qmt_warmup_convergence(
        codes=("SH.600000",),
        frequencies=("d", "1m"),
        as_of=AS_OF,
        snapshot_content_sha256=source,
        selected_candidates=(
            {
                "rank": 1,
                "code": "SH.600000",
                "source_position": 4,
                "lifecycle_stage": "approaching",
                "sector_horizontal_rank": 2,
                "point_type": "1buy",
                "selection_profile": "MODERN_BUY_REVIEW_ORDER",
            },
        ),
        parameter_document=parameters,
        frame_provider=frames,
        auditor=auditor,
    )

    view = candidate_warmup_presentation(report)

    assert view["source_content_sha256"] == source
    assert view["selected_candidate_count"] == 1
    assert view["candidates"]["SH.600000"]["status"] == "AVAILABLE"
    assert len(view["candidates"]["SH.600000"]["frequencies"]) == 2
    assert view["active_gate_unchanged"] is True
    assert view["candidate_identity_unchanged"] is True

    forged = dict(report)
    forged["source_content_sha256"] = "sha256:" + "e" * 64
    try:
        validate_candidate_warmup_diagnostic_document(forged)
    except ValueError as exc:
        assert "content hash mismatch" in str(exc)
    else:  # pragma: no cover - a forged source must never validate
        raise AssertionError("forged candidate diagnostic unexpectedly validated")
