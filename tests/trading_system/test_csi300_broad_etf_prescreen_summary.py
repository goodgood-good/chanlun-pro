from __future__ import annotations

from tools.summarize_csi300_broad_etf_prescreen import _decision_counts


def _decision(at: str, status: str, reasons=()):
    return {
        "alignment_decision_at": at,
        "status": status,
        "reason_codes": reasons,
    }


def test_decision_counts_support_calendar_year_and_frozen_splits() -> None:
    decisions = (
        _decision(
            "2020-01-02T10:00:00+08:00",
            "REJECT",
            ("NO_L2_LOCATOR_AT_FIRST_L1_RETURN_TERMINAL",),
        ),
        _decision("2021-05-07T10:00:00+08:00", "PASS"),
        _decision(
            "2022-03-16T10:00:00+08:00",
            "REJECT",
            ("NO_SUBSEQUENT_COMPLETED_L1_DOWN_RETURN",),
        ),
    )

    annual = _decision_counts(decisions, bucket="year")
    splits = _decision_counts(decisions, bucket="split")

    assert annual["2020"]["reject"] == 1
    assert annual["2021"]["pass"] == 1
    assert splits["TRAIN_60_PERCENT"]["candidates"] == 1
    assert splits["VALIDATION_20_PERCENT"]["pass"] == 1
    assert splits["FINAL_HOLDOUT_20_PERCENT"]["reject"] == 1


def test_split_counts_keep_zero_count_frozen_buckets() -> None:
    splits = _decision_counts(
        (_decision("2020-01-02T10:00:00+08:00", "PASS"),),
        bucket="split",
    )
    assert splits["VALIDATION_20_PERCENT"]["candidates"] == 0
    assert splits["FINAL_HOLDOUT_20_PERCENT"]["rejection_counts"] == {}
