#!/usr/bin/env python3
"""Append one human Chanlun judgement without creating any trading action."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Sequence
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.v3_human_review_screening import (  # noqa: E402
    HumanReviewAlert,
    HumanReviewFeedback,
    append_human_review_feedback,
    validate_human_review_feedback_causality,
    validate_human_review_screen_document,
)


CN = ZoneInfo("Asia/Shanghai")
DEFAULT_LEDGER = Path(".cache/chanlun_v3_human_review/feedback_ledger.json")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--screen-report", type=Path, required=True)
    value.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    value.add_argument("--candidate-id", required=True)
    value.add_argument("--reviewer", required=True)
    value.add_argument(
        "--request-id",
        help="stable idempotency key for retrying the same review write",
    )
    value.add_argument(
        "--reviewed-at",
        type=datetime.fromisoformat,
        default=None,
        help="timezone-aware ISO datetime; defaults to current Asia/Shanghai time",
    )
    value.add_argument(
        "--center",
        choices=("CONFIRMED", "REJECTED", "UNCERTAIN"),
        required=True,
    )
    value.add_argument(
        "--trend",
        choices=("UP", "DOWN", "CONSOLIDATION", "UNCERTAIN"),
        required=True,
    )
    value.add_argument(
        "--level",
        choices=("30M", "5M", "1M", "OTHER", "UNCERTAIN"),
        required=True,
    )
    value.add_argument(
        "--point",
        choices=(
            "BUY_1",
            "BUY_2",
            "BUY_3",
            "SELL_1",
            "SELL_2",
            "SELL_3",
            "NONE",
            "UNCERTAIN",
        ),
        required=True,
    )
    value.add_argument(
        "--disposition",
        choices=("WATCH", "REJECT", "PAPER_OBSERVE", "NEEDS_MORE_DATA"),
        required=True,
    )
    value.add_argument(
        "--decomposition",
        choices=("SAME_LEVEL", "CENTER", "COMBINED", "UNCERTAIN"),
        default="UNCERTAIN",
        help="human choice of same-level, center, or combined decomposition",
    )
    value.add_argument(
        "--center-expansion",
        choices=("CONFIRMED", "REJECTED", "UNCERTAIN"),
        default="UNCERTAIN",
    )
    value.add_argument(
        "--nine-segment-upgrade",
        choices=("CONFIRMED", "REJECTED", "UNCERTAIN"),
        default="UNCERTAIN",
    )
    value.add_argument(
        "--locator",
        choices=("CONFIRMED", "REJECTED", "UNCERTAIN"),
        default="UNCERTAIN",
    )
    value.add_argument("--notes", default="")
    return value


def _load_screen(
    path: Path,
) -> tuple[dict[str, object], tuple[HumanReviewAlert, ...]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("human review screen report cannot be read") from exc
    if not isinstance(payload, dict):
        raise ValueError("unsupported human review screen report")
    try:
        alerts = validate_human_review_screen_document(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    return payload, alerts


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: datetime | None = None,
) -> int:
    args = parser().parse_args(argv)
    report, alerts = _load_screen(args.screen_report.resolve())
    matches = tuple(
        alert for alert in alerts if alert.candidate_id == args.candidate_id
    )
    if len(matches) != 1:
        raise ValueError("candidate id is absent or non-unique in the source screen")
    source_sha256 = str(report["content_sha256"])
    observed_now = clock or datetime.now(CN)
    if observed_now.tzinfo is None:
        raise ValueError("review clock must be timezone-aware")
    reviewed_at = args.reviewed_at or observed_now
    if reviewed_at.tzinfo is None:
        raise ValueError("reviewed-at must be timezone-aware")
    if reviewed_at > observed_now:
        raise ValueError("reviewed-at cannot be in the future")
    feedback = HumanReviewFeedback(
        candidate_id=args.candidate_id,
        source_screen_content_sha256=source_sha256,
        reviewer=args.reviewer,
        reviewed_at=reviewed_at,
        center_judgement=args.center,
        trend_judgement=args.trend,
        level_judgement=args.level,
        point_judgement=args.point,
        disposition=args.disposition,
        decomposition_judgement=args.decomposition,
        center_expansion_judgement=args.center_expansion,
        nine_segment_upgrade_judgement=args.nine_segment_upgrade,
        locator_judgement=args.locator,
        notes=args.notes,
        request_id=args.request_id,
        signal_lifecycle_id=matches[0].signal_lifecycle_id,
    )
    validate_human_review_feedback_causality(
        feedback,
        matches[0],
        source_screen_content_sha256=source_sha256,
    )
    ledger = append_human_review_feedback(args.ledger.resolve(), feedback)
    print(
        json.dumps(
            {
                "ledger": str(args.ledger.resolve()),
                "feedback_id": feedback.feedback_id,
                "candidate_id": feedback.candidate_id,
                "entry_count": len(ledger["entries"]),
                "automated_order_authorized": False,
                "real_account_accessed": False,
                "live_status": "LIVE_DISABLED",
                "content_sha256": ledger["content_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
