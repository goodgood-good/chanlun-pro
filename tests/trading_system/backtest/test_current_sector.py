import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.decision_support.trading_system.backtest import current_sector as subject
from chanlun.decision_support.trading_system.backtest.current_sector import (
    CURRENT_GICS3_COMPOSITE_ADJUSTMENT,
    CURRENT_GICS3_COMPOSITE_PROVIDER,
    CurrentQmtGics3CompositeReplaySource,
    build_current_qmt_gics3_composite,
    current_composite_from_member_frames,
    deterministic_current_sector_composite_members,
    reclassify_current_sector_facts,
)
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_GICS3_COMPOSITE_ADJUSTMENT,
    QMT_GICS3_COMPOSITE_METHOD,
    QMT_GICS3_COMPOSITE_PROVIDER,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    SECTOR_FACT_SCHEMA,
    SectorResearchFacts,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    QmtFactorAt,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (
    QMTLocalKlineAudit,
)
from chanlun.decision_support.trading_system.models import SectorAssessment, TimeframeContext


CN = ZoneInfo("Asia/Shanghai")


def _frame(multiplier: float) -> pd.DataFrame:
    start = datetime(2025, 7, 25, 10, 0, tzinfo=CN)
    rows = []
    close = 10.0 * multiplier
    for index in range(4):
        opened = close
        close = opened * 1.01
        rows.append(
            {
                "time": int((start + timedelta(minutes=30 * index)).timestamp() * 1000),
                "open": opened,
                "high": close * 1.001,
                "low": opened * 0.999,
                "close": close,
                "volume": 1000,
            }
        )
    return pd.DataFrame(rows)


def test_current_sector_composite_uses_only_completed_member_rows() -> None:
    start = datetime(2025, 7, 25, 10, 0, tzinfo=CN)
    end = datetime(2025, 7, 25, 11, 30, tzinfo=CN)
    frames = {f"SH.6000{index:02d}": _frame(1 + index / 100) for index in range(8)}

    result = current_composite_from_member_frames(
        sector_id="qmt-gics3:test",
        member_frames=frames,
        factors_by_code={},
        eligible_member_count=8,
        start_at=start,
        end_at=end,
        minimum_member_count=8,
        minimum_bar_coverage=Decimal("1"),
    )

    # The first member row only establishes previous close; later completed
    # rows become ratios.  No future row is pulled into the requested end.
    assert tuple(result["date"]) == tuple(
        pd.Timestamp(start + timedelta(minutes=30 * index)) for index in range(1, 4)
    )
    assert set(result["volume"]) == {8.0}
    assert set(result["member_mask"]) == {(1 << 8) - 1}
    assert result.attrs["sector_membership_mode"] == (
        "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
    )
    assert result.attrs["sector_membership_scope"] == "CALLER_SUPPLIED"
    assert result.attrs["sector_composite_method"] == QMT_GICS3_COMPOSITE_METHOD
    assert CURRENT_GICS3_COMPOSITE_PROVIDER == QMT_GICS3_COMPOSITE_PROVIDER
    assert CURRENT_GICS3_COMPOSITE_ADJUSTMENT == QMT_GICS3_COMPOSITE_ADJUSTMENT
    assert result.attrs["sector_composite_member_path_revision"].startswith(
        "sha256:"
    )
    assert result.attrs["live_status"] == "LIVE_DISABLED"


def test_replay_source_reuses_raw_bars_but_rebuilds_each_visible_prefix(
    monkeypatch,
) -> None:
    start = datetime(2025, 7, 23, 0, 0, tzinfo=CN)
    end = datetime(2025, 7, 25, 11, 30, tzinfo=CN)
    codes = tuple(f"SH.6000{index:02d}" for index in range(8))
    frames = {code: _frame(1 + index / 100) for index, code in enumerate(codes)}
    daily_frames = {
        code: pd.DataFrame(
            {
                "time": [
                    int(
                        datetime(2025, 7, day, 0, 0, tzinfo=CN).timestamp()
                        * 1000
                    )
                    for day in (23, 24, 25)
                ],
                "open": (10.0, 10.1, 10.2),
                "high": (10.2, 10.3, 10.4),
                "low": (9.9, 10.0, 10.1),
                "close": (10.1, 10.2, 10.3),
                "volume": (1000, 1100, 1200),
            }
        )
        for code in codes
    }
    reads: list[str] = []

    def local_kline(**kwargs):
        code = kwargs["code"]
        reads.append(code)
        source = daily_frames if kwargs["frequency"] == "1d" else frames
        return source[code].copy(), {}

    monkeypatch.setattr(subject, "read_qmt_local_kline", local_kline)
    source = CurrentQmtGics3CompositeReplaySource(
        data_dir=Path("unused"),
        start_at=start,
        end_at=end,
        factors_by_code={},
    )
    early = source.five_minute_prefix(
        sector_id="qmt-gics3:test",
        member_codes=codes,
        observed_at=datetime(2025, 7, 25, 10, 30, tzinfo=CN),
    )
    later = source.five_minute_prefix(
        sector_id="qmt-gics3:test",
        member_codes=codes,
        observed_at=end,
    )

    assert len(reads) == len(codes)
    assert len(early) == 1
    assert len(later) == 3
    assert tuple(early["date"]) == tuple(later["date"].iloc[:1])
    assert early.attrs["sector_composite_member_path_revision"] != later.attrs[
        "sector_composite_member_path_revision"
    ]
    assert early.attrs["sector_membership_mode"] == (
        "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
    )
    native = source.native_daily_prefix(
        sector_id="qmt-gics3:test",
        member_codes=codes,
        observed_at=end,
    )
    assert len(reads) == len(codes) * 2
    assert tuple(value.time() for value in native["date"]) == (time(15),)
    assert tuple(value.date() for value in native["date"]) == (
        date(2025, 7, 24),
    )
    assert native.attrs["source_base_frequency"] == "native-d"
    assert native.attrs["sector_native_daily_role"] == (
        "UNRECONCILED_RESEARCH_MWD_ADVISORY_ONLY"
    )


def test_replay_source_proves_physical_qmt_left_boundary(
    monkeypatch,
) -> None:
    requested_start = datetime(2025, 7, 23, 0, 0, tzinfo=CN)
    observed_at = datetime(2025, 7, 25, 11, 30, tzinfo=CN)
    codes = tuple(f"SH.6000{index:02d}" for index in range(8))
    frames = {code: _frame(1 + index / 100) for index, code in enumerate(codes)}

    def local_kline(**kwargs):
        code = kwargs["code"]
        frame = frames[code].copy()
        first = datetime.fromtimestamp(int(frame.iloc[0]["time"]) / 1000, tz=CN)
        last = datetime.fromtimestamp(int(frame.iloc[-1]["time"]) / 1000, tz=CN)
        return frame, QMTLocalKlineAudit(
            code=code,
            frequency="5m",
            source_path=f"unused/{code}.DAT",
            source_sha256="sha256:" + "a" * 64,
            source_record_count=len(frame),
            selected_record_count=len(frame),
            first_at=first,
            last_at=last,
            source_first_at=first,
            source_last_at=last,
        )

    monkeypatch.setattr(subject, "read_qmt_local_kline", local_kline)
    source = CurrentQmtGics3CompositeReplaySource(
        data_dir=Path("unused"),
        start_at=requested_start,
        end_at=observed_at,
        factors_by_code={},
    )

    frame = source.five_minute_prefix(
        sector_id="qmt-gics3:test",
        member_codes=codes,
        observed_at=observed_at,
    )
    physical = frame.attrs["qmt_physical_five_minute_source_coverage"]

    assert physical["boundary_status"] == (
        "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
    )
    assert physical["requested_start_at"] == requested_start.isoformat()
    assert physical["required_contributor_physical_start_at"] == datetime(
        2025, 7, 25, 10, 0, tzinfo=CN
    ).isoformat()
    assert physical["representative_member_count"] == 8
    assert physical["available_member_file_count"] == 8
    assert physical["required_contributor_count"] == 8
    assert physical["decision_core_input"] is False
    assert str(physical["source_inventory_revision"]).startswith("sha256:")
    assert str(physical["audit_sha256"]).startswith("sha256:")
    # The audit is persisted in DataFrame.attrs by the chart archive.
    json.dumps(physical)


def test_current_sector_composite_fails_closed_on_insufficient_coverage() -> None:
    start = datetime(2025, 7, 25, 10, 0, tzinfo=CN)
    result = current_composite_from_member_frames(
        sector_id="qmt-gics3:test",
        member_frames={"SH.600000": _frame(1)},
        factors_by_code={},
        eligible_member_count=8,
        start_at=start,
        end_at=start + timedelta(hours=2),
    )

    assert result.empty
    assert result.attrs["data_grade"] == "RESEARCH_ONLY"


def test_current_sector_uses_same_causal_factor_and_excludes_future_event() -> None:
    closes = (
        datetime(2025, 8, 8, 15, 0, tzinfo=CN),
        datetime(2025, 8, 11, 10, 0, tzinfo=CN),
        datetime(2025, 8, 12, 10, 0, tzinfo=CN),
    )
    codes = tuple(f"SH.{600000 + index:06d}" for index in range(8))
    frames = {
        code: pd.DataFrame(
            {
                "time": [int(value.timestamp() * 1000) for value in closes],
                "open": (10.0, 5.0, 5.5),
                "high": (10.0, 5.0, 5.5),
                "low": (10.0, 5.0, 5.5),
                "close": (10.0, 5.0, 5.5),
                "volume": (1000, 1000, 1000),
            }
        )
        for code in codes
    }

    def factor(code: str, effective_on: date, divisor: str) -> QmtFactorAt:
        return QmtFactorAt(
            code=code,
            effective_on=effective_on,
            interest=Decimal("0"),
            stock_bonus=Decimal("0"),
            stock_gift=Decimal("1"),
            allot_num=Decimal("0"),
            allot_price=Decimal("0"),
            gugai=Decimal("0"),
            raw_price_divisor=Decimal(divisor),
        )

    causal = {
        code: (factor(code, date(2025, 8, 11), "2"),) for code in codes
    }
    with_future = {
        code: (
            *causal[code],
            factor(code, date(2025, 8, 13), "3"),
        )
        for code in codes
    }
    first = current_composite_from_member_frames(
        sector_id="qmt-gics3:test",
        member_frames=frames,
        factors_by_code=causal,
        eligible_member_count=8,
        start_at=closes[1],
        end_at=closes[2],
        minimum_member_count=8,
        minimum_bar_coverage=Decimal("1"),
    )
    second = current_composite_from_member_frames(
        sector_id="qmt-gics3:test",
        member_frames=frames,
        factors_by_code=with_future,
        eligible_member_count=8,
        start_at=closes[1],
        end_at=closes[2],
        minimum_member_count=8,
        minimum_bar_coverage=Decimal("1"),
    )

    assert tuple(first["close"]) == (1000.0, 1100.0)
    assert first.equals(second)
    assert first.attrs["sector_factor_revision"] == second.attrs[
        "sector_factor_revision"
    ]
    assert first.attrs["sector_factor_adjustment_contract_id"] == (
        "QMT_RAW_PRICE_DIVISOR_CAUSAL_EX_DATE"
    )


def test_current_sector_history_uses_live_sample_and_five_minute_first_derivation(
    monkeypatch,
) -> None:
    prior = datetime(2025, 7, 24, 15, 0, tzinfo=CN)
    session = datetime(2025, 7, 25, 9, 35, tzinfo=CN)
    closes = tuple(
        session.replace(hour=minute // 60, minute=minute % 60)
        for start, end in (
            (9 * 60 + 35, 11 * 60 + 30),
            (13 * 60 + 5, 15 * 60),
        )
        for minute in range(start, end + 1, 5)
    )
    members = tuple(f"SH.{600000 + index:06d}" for index in range(30))
    expected_sample = deterministic_current_sector_composite_members(
        "qmt-gics3:test",
        members,
    )
    calls: list[tuple[str, str]] = []

    def fake_read_qmt_local_kline(*, code, frequency, **_kwargs):
        calls.append((code, frequency))
        dates = (prior, *closes)
        offset = members.index(code) / 100
        frame = pd.DataFrame(
            {
                "time": [int(value.timestamp() * 1000) for value in dates],
                "open": [10 + offset + index / 1000 for index in range(len(dates))],
                "high": [10.2 + offset + index / 1000 for index in range(len(dates))],
                "low": [9.8 + offset + index / 1000 for index in range(len(dates))],
                "close": [10.1 + offset + index / 1000 for index in range(len(dates))],
                "volume": [1000.0] * len(dates),
            }
        )
        return frame, object()

    monkeypatch.setattr(
        subject,
        "read_qmt_local_kline",
        fake_read_qmt_local_kline,
    )

    result = build_current_qmt_gics3_composite(
        data_dir=Path("unused"),
        sector_id="qmt-gics3:test",
        member_codes=members,
        factors_by_code={},
        start_at=prior,
        end_at=closes[-1],
    )

    assert tuple(code for code, _frequency in calls) == expected_sample
    assert {frequency for _code, frequency in calls} == {"5m"}
    assert len(calls) == 24
    assert len(result) == 8
    assert result.attrs["sector_members"] == members
    assert result.attrs["sector_composite_members"] == expected_sample
    assert result.attrs["source_base_frequency"] == "5m"
    assert result.attrs["derived_frequency"] == "30m"
    assert result.attrs["sector_thirty_minute_derivation_contract"] == (
        "SIX_CONTIGUOUS_COMPLETED_5M_COMPOSITE_BARS"
    )


def test_current_sector_reclassification_registers_explicit_backfill_source() -> None:
    observed = datetime(2025, 7, 25, 10, 30, tzinfo=CN)
    context = TimeframeContext(
        "30m", "up", "supportive", False, None, None, (), observed
    )
    neutral = TimeframeContext(
        "5m", "neutral", "neutral", False, None, None, (), observed
    )
    blocked = SectorAssessment(
        "qmt-gics3:test",
        "test",
        False,
        True,
        "hostile",
        (),
        ("non_native_sector_kline",),
        context,
        neutral,
        TimeframeContext(
            "1m", "neutral", "neutral", False, None, None, (), observed
        ),
    )
    facts = SectorResearchFacts(
        schema=SECTOR_FACT_SCHEMA,
        algorithm_revision="sha256:" + "1" * 64,
        source_revision="sha256:" + "2" * 64,
        sector_id="qmt-gics3:test",
        sector_name="test",
        member_count=8,
        row_count=1,
        thirty_points=(),
        assessments=((observed, blocked),),
    )
    frame = pd.DataFrame(
        {
            "date": [pd.Timestamp(observed)],
            "open": [1.0],
            "high": [1.1],
            "low": [0.9],
            "close": [1.05],
            "volume": [8.0],
        }
    )

    result = reclassify_current_sector_facts(
        facts=facts,
        frame=frame,
        expected_closes=(observed,),
        algorithm_revision="sha256:" + "3" * 64,
        source_revision=facts.source_revision,
    )

    assert result.assessments[0][1].reason_codes != ("non_native_sector_kline",)
    assert result.assessments[0][1].hard_block is False
