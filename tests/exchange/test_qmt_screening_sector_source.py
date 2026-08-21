from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.exchange import qmt_screening_sector_source as subject
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_CURRENT_A_SHARE_SECTOR,
    QMT_GICS3_CATALOG_SOURCE,
    QMT_GICS3_COMPOSITE_PROVIDER,
    QMT_GICS_HIERARCHY_CATALOG_SOURCE,
    QmtSectorCompositeSource,
    QmtSectorStrengthSource,
    build_qmt_gics_hierarchy_sector_catalog,
    build_qmt_gics3_sector_catalog,
    build_qmt_gics3_sector_catalog_from_local_files,
    qmt_gics_hierarchy_catalog_revision,
)
from chanlun.decision_support.fingerprints import sha256_json


SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 23, 10, 30, tzinfo=SHANGHAI)


def test_qmt_fact_ast_identity_isolates_independent_fact_families() -> None:
    original = """
SHARED = 1
DAILY_ONLY = 3
def shared(value):
    return value + SHARED
class Frames:
    def build(self):
        return shared(1)
class Daily:
    def build(self):
        return shared(2) + DAILY_ONLY
"""
    daily_changed = original.replace("DAILY_ONLY = 3", "DAILY_ONLY = 4")
    shared_changed = original.replace("SHARED = 1", "SHARED = 2")

    frames = subject._producer_ast_manifest(original, roots=("Frames",))
    daily = subject._producer_ast_manifest(original, roots=("Daily",))
    assert subject._producer_ast_manifest(
        daily_changed,
        roots=("Frames",),
    ) == frames
    assert subject._producer_ast_manifest(
        daily_changed,
        roots=("Daily",),
    ) != daily
    assert subject._producer_ast_manifest(
        shared_changed,
        roots=("Frames",),
    ) != frames
    assert subject._producer_ast_manifest(
        shared_changed,
        roots=("Daily",),
    ) != daily


def test_qmt_fact_ast_identity_excludes_only_declared_operational_cache_values() -> (
    None
):
    original = """
CACHE_CAPACITY = 12
SEMANTIC_LIMIT = 8
class Frames:
    def build(self):
        return CACHE_CAPACITY + SEMANTIC_LIMIT
"""
    cache_changed = original.replace("CACHE_CAPACITY = 12", "CACHE_CAPACITY = 256")
    semantic_changed = original.replace("SEMANTIC_LIMIT = 8", "SEMANTIC_LIMIT = 9")
    options = {
        "roots": ("Frames",),
        "excluded_names": frozenset({"CACHE_CAPACITY"}),
    }

    manifest = subject._producer_ast_manifest(original, **options)

    assert subject._producer_ast_manifest(cache_changed, **options) == manifest
    assert subject._producer_ast_manifest(semantic_changed, **options) != manifest
    assert subject._producer_ast_manifest(
        cache_changed,
        roots=("Frames",),
    ) != subject._producer_ast_manifest(original, roots=("Frames",))


def test_qmt_fact_families_have_distinct_authenticated_revisions() -> None:
    composite = subject.qmt_sector_composite_fact_producer_revision()
    daily = subject.qmt_sector_daily_fact_producer_revision()

    assert composite.startswith("sha256:") and len(composite) == 71
    assert daily.startswith("sha256:") and len(daily) == 71
    assert composite != daily


def test_qmt_fact_dependencies_do_not_cross_intraday_and_daily_families(
    monkeypatch,
) -> None:
    original_read_bytes = Path.read_bytes
    touched: list[str] = []

    def recording_read_bytes(path: Path) -> bytes:
        touched.append(path.name)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)

    subject.qmt_sector_composite_fact_producer_revision()
    assert "qmt_time_contract.py" in touched
    assert "qmt_sector_same_base.py" in touched
    assert "etf_proxy_facts.py" not in touched

    touched.clear()
    subject.qmt_sector_daily_fact_producer_revision()
    assert "etf_proxy_facts.py" in touched
    assert "qmt_time_contract.py" not in touched
    assert "qmt_sector_same_base.py" not in touched


class FakeXtdata:
    def __init__(
        self,
        *,
        market_end: datetime = AS_OF,
        latest_probe_end: datetime | None = None,
    ) -> None:
        self.market_calls = 0
        self.download_calls: list[dict[str, object]] = []
        self.market_stock_lists: list[tuple[str, ...]] = []
        self.trading_date_calls: list[tuple[str, str]] = []
        self.factor_calls: list[tuple[str, str, str]] = []
        self.market_end = market_end
        self.latest_probe_end = latest_probe_end or market_end
        self.members = {
            QMT_CURRENT_A_SHARE_SECTOR: [
                "600000.SH",
                "000001.SZ",
                "430047.BJ",
            ],
            "GICS3商业银行": [
                "600000.SH",
                "000001.SZ",
                "430047.BJ",
                "600001.SH",
                "600000.SH",
                "00700.HK",
                "AAPL.US",
            ],
            "GICS1金融": ["600000.SH", "000001.SZ", "430047.BJ"],
        }

    def get_sector_list(self):
        return ["普通板块", "GICS1金融", "GICS3商业银行"]

    def get_stock_list_in_sector(self, name, real_timetag=-1):
        assert real_timetag == -1
        return list(self.members[name])

    def download_history_data(self, stock_code, period, **kwargs):
        self.download_calls.append(
            {"stock_code": stock_code, "period": period, **kwargs}
        )
        return None

    def get_divid_factors(self, stock_code, start_time="", end_time=""):
        self.factor_calls.append((stock_code, start_time, end_time))
        return pd.DataFrame()

    def get_trading_dates(self, market, start_time, end_time, count):
        assert market == "SH"
        assert start_time < end_time
        assert count == -1
        self.trading_date_calls.append((start_time, end_time))
        return [
            int(
                datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI)
                .timestamp()
                * 1000
            )
            for day in (AS_OF.date() - timedelta(days=1), AS_OF.date())
        ]

    def get_market_data(self, **kwargs):
        self.market_calls += 1
        codes = tuple(kwargs["stock_list"])
        self.market_stock_lists.append(codes)
        market_end = (
            self.latest_probe_end
            if kwargs["field_list"] == ["time"]
            else self.market_end
        )
        dates = tuple(
            market_end - timedelta(minutes=5 * offset)
            for offset in range(10, -1, -1)
        )
        native_times = [int(value.timestamp() * 1000) for value in dates]
        columns = tuple(range(len(dates)))
        fields: dict[str, pd.DataFrame] = {}
        for field in ("time", "open", "high", "low", "close", "volume"):
            rows = []
            for member_index, _code in enumerate(codes):
                base = 10.0 + member_index
                if field == "time":
                    rows.append(native_times)
                    continue
                values = []
                for bar_index in range(len(dates)):
                    close = base * (1.0 + bar_index * 0.001)
                    values.append(
                        {
                            "open": close * 0.999,
                            "high": close * 1.002,
                            "low": close * 0.998,
                            "close": close,
                            "volume": 1000.0 + bar_index,
                        }[field]
                    )
                rows.append(values)
            fields[field] = pd.DataFrame(rows, index=codes, columns=columns)
        return fields


def test_qmt_catalog_uses_only_qmt_gics3_a_share_members(monkeypatch) -> None:
    fake = FakeXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    catalog = build_qmt_gics3_sector_catalog(captured_at=AS_OF)

    assert catalog["source"] == QMT_GICS3_CATALOG_SOURCE
    assert catalog["captured_at"] == AS_OF.isoformat()
    assert catalog["point_in_time_scope"] == "CURRENT_CAPTURE_ONLY"
    assert len(catalog["sectors"]) == 1
    [sector] = catalog["sectors"]
    assert sector["source_key"] == "GICS3商业银行"
    assert sector["name"] == "商业银行"
    assert sector["sector_id"].startswith("qmt-gics3:")
    assert sector["member_codes"] == ["BJ.430047", "SH.600000", "SZ.000001"]
    evidence = catalog["capture_evidence"]
    assert evidence["membership_universe_filter_applied"] is True
    assert evidence["membership_universe_source"] == (
        f"QMT_RPC:{QMT_CURRENT_A_SHARE_SECTOR}"
    )
    assert evidence["membership_universe_member_count"] == 3
    assert evidence["unfiltered_gics3_member_count"] == 4
    assert evidence["included_gics3_member_count"] == 3
    assert evidence["excluded_noncurrent_member_count"] == 1
    assert str(evidence["excluded_noncurrent_members_sha256"]).startswith("sha256:")


def test_qmt_catalog_fails_closed_without_current_a_share_universe(
    monkeypatch,
) -> None:
    fake = FakeXtdata()
    fake.members[QMT_CURRENT_A_SHARE_SECTOR] = []
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    with pytest.raises(RuntimeError, match="current A-share universe"):
        build_qmt_gics3_sector_catalog(captured_at=AS_OF)


def test_qmt_hierarchy_catalog_maps_gics4_to_one_gics3_parent(
    monkeypatch,
) -> None:
    fake = FakeXtdata()
    fake.members[QMT_CURRENT_A_SHARE_SECTOR].append("600002.SH")
    fake.members["GICS3EmptyCurrentAShare"] = ["AAPL.US"]
    fake.members["GICS4EmptyCurrentAShare"] = ["AAPL.US"]
    fake.members["GICS4股份制银行"] = [
        "600000.SH",
        "000001.SZ",
        "AAPL.US",
    ]
    fake.members["GICS4其他银行"] = ["430047.BJ", "600002.SH"]
    fake.members["GICS4商业银行"] = [
        "600000.SH",
        "000001.SZ",
        "430047.BJ",
    ]
    monkeypatch.setattr(
        fake,
        "get_sector_list",
        lambda: [
            "普通板块",
            "GICS3EmptyCurrentAShare",
            "GICS4EmptyCurrentAShare",
            "GICS4其他银行",
            "GICS3商业银行",
            "GICS4商业银行",
            "GICS4股份制银行",
        ],
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    catalog = build_qmt_gics_hierarchy_sector_catalog(captured_at=AS_OF)

    assert catalog["source"] == QMT_GICS_HIERARCHY_CATALOG_SOURCE
    assert catalog["catalog_revision"] == qmt_gics_hierarchy_catalog_revision(
        catalog
    )
    assert str(catalog["gics3_catalog_revision"]).startswith("sha256:")
    parents = [
        row for row in catalog["sectors"] if row["taxonomy_level"] == "GICS3"
    ]
    children = [
        row for row in catalog["sectors"] if row["taxonomy_level"] == "GICS4"
    ]
    assert len(parents) == 1
    assert len(children) == 2
    [parent] = parents
    assert parent["parent_sector_id"] is None
    assert parent["member_codes"] == ["BJ.430047", "SH.600000", "SZ.000001"]
    assert all(row["parent_sector_id"] == parent["sector_id"] for row in children)
    assert {row["name"] for row in children} == {
        "商业银行 → 其他银行",
        "商业银行 → 股份制银行",
    }
    other = next(row for row in children if row["source_key"] == "GICS4其他银行")
    assert other["member_codes"] == ["BJ.430047"]
    evidence = catalog["capture_evidence"]
    assert evidence["gics3_sector_count"] == 1
    assert evidence["gics4_sector_count"] == 2
    assert evidence["gics4_parent_relation_count"] == 2
    assert evidence["collapsed_degenerate_gics4_sector_count"] == 1
    assert str(
        evidence["collapsed_degenerate_gics4_source_keys_sha256"]
    ).startswith("sha256:")
    assert evidence["excluded_empty_gics3_sector_count"] == 1
    assert evidence["excluded_empty_gics4_sector_count"] == 1
    assert str(evidence["excluded_empty_gics3_source_keys_sha256"]).startswith(
        "sha256:"
    )
    assert str(evidence["excluded_empty_gics4_source_keys_sha256"]).startswith(
        "sha256:"
    )
    assert evidence["hierarchy_orphan_member_count"] == 1


def test_qmt_local_catalog_is_read_only_deterministic_capture(
    tmp_path: Path,
) -> None:
    sector_dir = tmp_path / "Sector" / "Temple" / "GICS"
    sector_dir.mkdir(parents=True)
    (sector_dir / "GICS3商业银行").write_bytes(
        "600000.SH,000001.SZ,430047.BJ,00700.HK,600000.SH,".encode("gb18030")
    )
    (sector_dir / "GICS1金融").write_bytes("600000.SH".encode("gb18030"))

    first = build_qmt_gics3_sector_catalog_from_local_files(
        qmt_data_dir=tmp_path,
        captured_at=AS_OF,
    )
    second = build_qmt_gics3_sector_catalog_from_local_files(
        qmt_data_dir=tmp_path,
        captured_at=AS_OF,
    )

    assert first == second
    assert first["capture_transport"] == "QMT_LOCAL_SECTOR_FILES"
    assert first["point_in_time_scope"] == "CURRENT_CAPTURE_ONLY"
    assert len(first["sectors"]) == 1
    [sector] = first["sectors"]
    assert sector["source_key"] == "GICS3商业银行"
    assert sector["member_codes"] == ["BJ.430047", "SH.600000", "SZ.000001"]
    evidence = first["capture_evidence"]
    assert evidence["source_file_count"] == 1
    assert evidence["source_manifest_sha256"].startswith("sha256:")
    assert evidence["source_directory"] == str(sector_dir.resolve())
    assert evidence["membership_universe_filter_applied"] is False
    assert evidence["membership_universe_source"] == (
        "UNAVAILABLE_IN_LOCAL_GICS3_FILES"
    )


@pytest.mark.parametrize("sector_id", ("qmt-gics3:test", "qmt-gics4:test"))
def test_qmt_component_source_builds_and_caches_auditable_sector_frame(
    monkeypatch,
    sector_id: str,
) -> None:
    fake = FakeXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    progress: list[int] = []
    source = QmtSectorCompositeSource(
        minimum_member_count=8,
        progress_callback=lambda: progress.append(len(progress)),
    )
    members = tuple(f"SH.6000{index:02d}" for index in range(8))

    first = source.frame(
        sector_id=sector_id,
        sector_name="测试行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )
    progress_after_first = len(progress)
    second = source.frame(
        sector_id=sector_id,
        sector_name="测试行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )
    smaller = source.frame(
        sector_id=sector_id,
        sector_name="测试行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=2,
    )

    assert fake.market_calls == 2
    calendar_start, calendar_end = fake.trading_date_calls[0]
    assert calendar_end == AS_OF.strftime("%Y%m%d")
    assert (
        AS_OF.date()
        - datetime.strptime(calendar_start, "%Y%m%d").date()
    ).days >= 1000
    assert progress_after_first >= 6
    assert len(progress) == progress_after_first
    assert len(fake.download_calls) == len(members)
    assert len(fake.factor_calls) == len(members)
    assert source.cache_health_snapshot()["frame_capacity"] == 8
    assert fake.download_calls[0]["period"] == "5m"
    assert fake.download_calls[0]["incrementally"] is True
    assert fake.download_calls[0]["end_time"] == "20260723103001"
    assert len(first) == 4
    assert first.equals(second)
    assert smaller.equals(first.tail(2).reset_index(drop=True))
    assert smaller.attrs["sector_composite_member_path_revision"] != (
        first.attrs["sector_composite_member_path_revision"]
    )
    assert first["date"].is_monotonic_increasing
    assert (first["high"] >= first[["open", "close"]].max(axis=1)).all()
    assert (first["low"] <= first[["open", "close"]].min(axis=1)).all()
    assert first.attrs["price_basis_provider"] == QMT_GICS3_COMPOSITE_PROVIDER
    assert first.attrs["price_basis_adjustment"] == (
        "causal-factor-stable-24-member-median"
    )
    assert first.attrs["structure_price_quantum"] == "0.000001"
    assert first.attrs["price_basis_revision"].startswith("sha256:")
    assert first.attrs["sector_membership_revision"].startswith("sha256:")
    assert first.attrs["sector_membership_scope"] == "CALLER_SUPPLIED"
    assert first.attrs["sector_members"] == members
    assert first.attrs["sector_composite_members"] == members
    assert first.attrs["sector_composite_member_limit"] == 24
    assert first.attrs["sector_composite_minimum_member_count"] == 8
    assert first.attrs["sector_composite_minimum_bar_coverage"] == "0.60"
    assert first.attrs["sector_composite_required_member_count"] == 8
    assert first.attrs["sector_composite_member_mask_contract"] == (
        "BIT_I_IS_SECTOR_COMPOSITE_MEMBERS_I"
    )
    assert first.attrs["sector_composite_member_path_revision"].startswith(
        "sha256:"
    )
    assert (first["member_mask"] == (1 << len(members)) - 1).all()
    assert first.attrs["sector_composite_method"] == (
        "DETERMINISTIC_HASH_SAMPLE_CAUSAL_FACTOR_MEDIAN_RETURN_CHAIN"
    )
    assert first.attrs["sector_factor_adjustment_contract_id"] == (
        "QMT_RAW_PRICE_DIVISOR_CAUSAL_EX_DATE"
    )
    assert first.attrs["sector_factor_revision"].startswith("sha256:")


def test_composite_memory_frame_cache_is_strictly_bounded() -> None:
    source = QmtSectorCompositeSource()

    for index in range(12):
        source._remember_frame(  # noqa: SLF001 - verifies the LRU safety bound
            (f"sector-{index}", "5m", index),
            (index, f"revision-{index}", pd.DataFrame({"close": [index + 1]})),
        )

    health = source.cache_health_snapshot()
    assert health["frame_entries"] == health["frame_capacity"] == 8
    assert tuple(source._cache) == tuple(  # noqa: SLF001
        (f"sector-{index}", "5m", index) for index in range(4, 12)
    )


def test_qmt_component_source_builds_native_daily_research_advisory(
    monkeypatch,
) -> None:
    observed = datetime(2026, 7, 23, 15, 1, tzinfo=SHANGHAI)
    sessions = (
        date(2026, 7, 20),
        date(2026, 7, 21),
        date(2026, 7, 22),
        date(2026, 7, 23),
    )

    class DailyXtdata(FakeXtdata):
        def get_trading_dates(self, market, start_time, end_time, count):
            assert market == "SH" and count == -1
            return [
                int(
                    datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI)
                    .timestamp()
                    * 1000
                )
                for day in sessions
            ]

        def get_market_data(self, **kwargs):
            assert kwargs["period"] == "1d"
            self.market_calls += 1
            codes = tuple(kwargs["stock_list"])
            self.market_stock_lists.append(codes)
            timestamps = [
                int(
                    datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI)
                    .timestamp()
                    * 1000
                )
                for day in sessions
            ]
            fields: dict[str, pd.DataFrame] = {}
            for field in kwargs["field_list"]:
                rows = []
                for member_index, _code in enumerate(codes):
                    values = []
                    for bar_index, timestamp in enumerate(timestamps):
                        close = 10.0 + member_index + bar_index
                        values.append(
                            timestamp
                            if field == "time"
                            else {
                                "open": close - 0.2,
                                "high": close + 0.4,
                                "low": close - 0.4,
                                "close": close,
                                "volume": 1000.0 + bar_index,
                            }[field]
                        )
                    rows.append(values)
                fields[field] = pd.DataFrame(rows, index=codes)
            return fields

    fake = DailyXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))

    frame = QmtSectorCompositeSource(minimum_member_count=8).frame(
        sector_id="qmt-gics3:daily-advisory",
        sector_name="daily-advisory",
        members=members,
        frequency="1d",
        as_of=observed,
        request_bars=3,
    )

    assert len(frame) == 3
    assert tuple(value.time() for value in frame["date"]) == (
        datetime.min.replace(hour=15).time(),
    ) * 3
    assert frame.attrs["source_base_frequency"] == "native-d"
    assert frame.attrs["derived_frequency"] == "d"
    assert frame.attrs["sector_native_daily_role"] == (
        "UNRECONCILED_RESEARCH_MWD_ADVISORY_ONLY"
    )
    assert frame.attrs["source_base_stream_revision"].startswith("sha256:")
    assert all(value["period"] == "1d" for value in fake.download_calls)


def test_qmt_component_source_neutralizes_ex_date_jump_causally(
    monkeypatch,
) -> None:
    class CorporateActionXtdata(FakeXtdata):
        def get_divid_factors(self, stock_code, start_time="", end_time=""):
            self.factor_calls.append((stock_code, start_time, end_time))
            # Deliberately return a future event too.  The adapter must not use
            # it even if a provider ignores the requested end_time.
            return pd.DataFrame(
                [
                    {
                        "interest": 0,
                        "stockBonus": 0,
                        "stockGift": 1,
                        "allotNum": 0,
                        "allotPrice": 0,
                        "gugai": 0,
                        "dr": 2,
                    },
                    {
                        "interest": 0,
                        "stockBonus": 0,
                        "stockGift": 2,
                        "allotNum": 0,
                        "allotPrice": 0,
                        "gugai": 0,
                        "dr": 3,
                    },
                ],
                index=(AS_OF.date().isoformat(), "2026-07-24"),
            )

        def get_market_data(self, **kwargs):
            self.market_calls += 1
            codes = tuple(kwargs["stock_list"])
            self.market_stock_lists.append(codes)
            closes = (
                datetime(2026, 7, 22, 15, 0, tzinfo=SHANGHAI),
                datetime(2026, 7, 23, 10, 25, tzinfo=SHANGHAI),
                datetime(2026, 7, 23, 10, 30, tzinfo=SHANGHAI),
            )
            prices = (10.0, 5.0, 5.5)
            fields: dict[str, pd.DataFrame] = {}
            for field in kwargs["field_list"]:
                if field == "time":
                    values = [int(value.timestamp() * 1000) for value in closes]
                elif field == "volume":
                    values = [1000.0] * len(closes)
                else:
                    values = list(prices)
                fields[field] = pd.DataFrame([values] * len(codes), index=codes)
            return fields

    fake = CorporateActionXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))

    result = QmtSectorCompositeSource(minimum_member_count=8).frame(
        sector_id="qmt-gics3:factor",
        sector_name="公司行为测试",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=2,
    )

    assert tuple(result["close"]) == (1000.0, 1100.0)
    assert len(fake.factor_calls) == len(members)
    assert all(call[2] == "20260723" for call in fake.factor_calls)
    assert result.attrs["sector_factor_adjustment_contract_id"] == (
        "QMT_RAW_PRICE_DIVISOR_CAUSAL_EX_DATE"
    )


def test_qmt_component_source_rejects_missing_factor_ledger(monkeypatch) -> None:
    class FailingFactorsXtdata(FakeXtdata):
        def get_divid_factors(self, *_args, **_kwargs):
            raise RuntimeError("factor service unavailable")

    fake = FailingFactorsXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    with pytest.raises(RuntimeError, match="causal factor ledger unavailable"):
        QmtSectorCompositeSource(minimum_member_count=8).frame(
            sector_id="qmt-gics3:factor-failure",
            sector_name="因子失败测试",
            members=tuple(f"SH.6000{index:02d}" for index in range(8)),
            frequency="5m",
            as_of=AS_OF,
            request_bars=2,
        )


def test_qmt_component_source_fails_closed_when_latest_session_is_stale(
    monkeypatch,
) -> None:
    fake = FakeXtdata(market_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    source = QmtSectorCompositeSource(minimum_member_count=8)

    frame = source.frame(
        sector_id="qmt-gics3:stale",
        sector_name="陈旧行业",
        members=tuple(f"SH.6000{index:02d}" for index in range(8)),
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )

    assert frame.empty
    assert frame.attrs["price_basis_provider"] == QMT_GICS3_COMPOSITE_PROVIDER


def test_qmt_component_source_repairs_fresh_but_shallow_history_once_per_bucket(
    monkeypatch,
) -> None:
    """A current last bar must not hide an underfilled M/W/D warmup prefix."""

    class ShallowThenDownloadedXtdata(FakeXtdata):
        def get_market_data(self, **kwargs):
            self.market_calls += 1
            codes = tuple(kwargs["stock_list"])
            self.market_stock_lists.append(codes)
            downloaded = {
                str(value["stock_code"])
                for value in self.download_calls
            }
            deep = all(code in downloaded for code in codes)
            available = 30 if deep else 5
            closes = tuple(
                close
                for day in (AS_OF.date() - timedelta(days=1), AS_OF.date())
                for close in QmtSectorCompositeSource._session_closes(day, "5m")
                if close <= AS_OF
            )[-available:]
            native_times = [int(value.timestamp() * 1000) for value in closes]
            fields: dict[str, pd.DataFrame] = {}
            for field in kwargs["field_list"]:
                rows = []
                for member_index, _code in enumerate(codes):
                    base = 10.0 + member_index
                    if field == "time":
                        rows.append(native_times)
                        continue
                    values = []
                    for bar_index in range(len(closes)):
                        close = base * (1.0 + bar_index * 0.001)
                        values.append(
                            {
                                "open": close * 0.999,
                                "high": close * 1.002,
                                "low": close * 0.998,
                                "close": close,
                                "volume": 1000.0 + bar_index,
                            }[field]
                        )
                    rows.append(values)
                fields[field] = pd.DataFrame(rows, index=codes)
            return fields

    fake = ShallowThenDownloadedXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    source = QmtSectorCompositeSource(minimum_member_count=8)
    members = tuple(f"SH.6000{index:02d}" for index in range(8))

    first = source.frame(
        sector_id="qmt-gics3:shallow-one",
        sector_name="浅历史行业一",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=16,
    )
    second = source.frame(
        sector_id="qmt-gics3:shallow-two",
        sector_name="浅历史行业二",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=16,
    )

    assert len(first) == len(second) == 16
    assert len(fake.download_calls) == len(members)
    assert all(value["period"] == "5m" for value in fake.download_calls)
    assert all(value["incrementally"] is False for value in fake.download_calls)
    expected = tuple(
        close
        for day in (AS_OF.date() - timedelta(days=1), AS_OF.date())
        for close in QmtSectorCompositeSource._session_closes(day, "5m")
        if close <= AS_OF
    )[-17:]
    assert {
        (value["start_time"], value["end_time"])
        for value in fake.download_calls
    } == {
        (
            expected[0].strftime("%Y%m%d%H%M%S"),
            (expected[-1] + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S"),
        )
    }


def test_qmt_component_source_downloads_the_inclusive_final_close(
    monkeypatch,
) -> None:
    """盘后增量下载必须补齐 QMT 不包含结束端点所遗漏的收盘柱。"""

    class ExclusiveDownloadXtdata(FakeXtdata):
        def download_history_data(self, stock_code, period, **kwargs):
            super().download_history_data(stock_code, period, **kwargs)
            downloaded_through = datetime.strptime(
                kwargs["end_time"], "%Y%m%d%H%M%S"
            ).replace(tzinfo=SHANGHAI)
            if downloaded_through > AS_OF:
                self.market_end = AS_OF
            return None

    fake = ExclusiveDownloadXtdata(
        market_end=AS_OF - timedelta(minutes=5),
        latest_probe_end=AS_OF - timedelta(minutes=5),
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    frame = QmtSectorCompositeSource(minimum_member_count=8).frame(
        sector_id="qmt-gics3:exclusive-download-end",
        sector_name="下载端点测试",
        members=tuple(f"SH.6000{index:02d}" for index in range(8)),
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )

    assert not frame.empty
    assert frame["date"].iloc[-1].to_pydatetime() == AS_OF
    assert {value["end_time"] for value in fake.download_calls} == {
        "20260723103001"
    }


def test_qmt_component_source_uses_stable_bounded_member_sample(
    monkeypatch,
) -> None:
    fake = FakeXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    source = QmtSectorCompositeSource(minimum_member_count=8)
    members = tuple(f"SH.600{index:03d}" for index in range(40))

    frame = source.frame(
        sector_id="qmt-gics3:bounded",
        sector_name="大行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )

    assert len(frame) == 4
    assert len(fake.download_calls) == 24
    assert all(len(codes) == 24 for codes in fake.market_stock_lists)
    assert frame.attrs["sector_members"] == members
    assert len(frame.attrs["sector_composite_members"]) == 24
    assert set(frame.attrs["sector_composite_members"]).issubset(members)
    assert frame.attrs["sector_composite_required_member_count"] == 15
    assert (frame["member_mask"] == (1 << 24) - 1).all()


def test_qmt_component_source_anchors_coverage_to_frozen_sample(
    monkeypatch,
) -> None:
    class PartialHistoryXtdata(FakeXtdata):
        def get_market_data(self, **kwargs):
            result = super().get_market_data(**kwargs)
            if kwargs["field_list"] == ["time"]:
                return result
            # QMT returned usable bars for only eight of the requested 24
            # deterministic representatives.  The frozen denominator keeps
            # coverage at 8/24 instead of survivor-biased 8/8.
            return {
                field: values.iloc[:8].copy()
                for field, values in result.items()
            }

    fake = PartialHistoryXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    source = QmtSectorCompositeSource(minimum_member_count=8)
    members = tuple(f"SH.600{index:03d}" for index in range(40))

    frame = source.frame(
        sector_id="qmt-gics3:partial-history",
        sector_name="历史缺失行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )

    assert frame.empty
    assert len(fake.market_stock_lists[-1]) == 24
    assert frame.attrs["sector_composite_members"]


def test_qmt_component_source_rejects_interior_five_minute_grid_gap(
    monkeypatch,
) -> None:
    class InteriorGapXtdata(FakeXtdata):
        def get_market_data(self, **kwargs):
            result = super().get_market_data(**kwargs)
            if kwargs["field_list"] == ["time"]:
                return result
            return {
                field: values.drop(columns=values.columns[-2])
                for field, values in result.items()
            }

    fake = InteriorGapXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))

    frame = QmtSectorCompositeSource(minimum_member_count=8).frame(
        sector_id="qmt-gics3:interior-gap",
        sector_name="中间缺柱行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )

    assert frame.empty
    assert frame.attrs["sector_composite_members"] == members


def test_qmt_component_source_trims_prefix_before_an_old_grid_gap(
    monkeypatch,
) -> None:
    """旧缺口前缀可裁掉，但最新连续后缀仍须逐根匹配交易日历。"""

    class OldGapXtdata(FakeXtdata):
        def get_market_data(self, **kwargs):
            result = super().get_market_data(**kwargs)
            if kwargs["field_list"] == ["time"]:
                return result
            return {
                field: values.drop(columns=values.columns[3])
                for field, values in result.items()
            }

    fake = OldGapXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))

    frame = QmtSectorCompositeSource(minimum_member_count=8).frame(
        sector_id="qmt-gics3:old-prefix-gap",
        sector_name="旧前缀缺口行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=16,
    )

    assert len(frame) == 7
    assert frame["date"].iloc[0].to_pydatetime() == AS_OF - timedelta(minutes=30)
    assert frame["date"].iloc[-1].to_pydatetime() == AS_OF
    expected_suffix = tuple(
        AS_OF - timedelta(minutes=5 * offset)
        for offset in range(6, -1, -1)
    )
    assert tuple(value.to_pydatetime() for value in frame["date"]) == (
        expected_suffix
    )


def test_qmt_component_fact_cache_survives_a_new_source_instance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The persisted input frame is reusable, but no structure result is cached."""

    fake = FakeXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))
    revision = "sha256:" + "a" * 64

    first = QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(
        sector_id="qmt-gics3:persistent",
        sector_name="Persistent",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )
    calls_after_first = fake.market_calls
    downloads_after_first = len(fake.download_calls)

    second = QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(
        sector_id="qmt-gics3:persistent",
        sector_name="Persistent",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )

    pd.testing.assert_frame_equal(first, second, check_exact=True)
    assert first.attrs == second.attrs
    assert fake.market_calls == calls_after_first
    assert len(fake.download_calls) == downloads_after_first
    assert len(tuple(tmp_path.glob("*.json"))) == 1

    next_close = AS_OF + timedelta(minutes=5)
    fake.market_end = next_close
    fake.latest_probe_end = next_close - timedelta(days=1)
    QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(
        sector_id="qmt-gics3:persistent",
        sector_name="Persistent",
        members=members,
        frequency="5m",
        as_of=next_close,
        request_bars=4,
    )
    assert fake.market_calls > calls_after_first
    calls_after_time_miss = fake.market_calls

    # Producer identity and the exact member set are part of the fact identity.
    QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision="sha256:" + "b" * 64,
    ).frame(
        sector_id="qmt-gics3:persistent",
        sector_name="Persistent",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )
    assert fake.market_calls > calls_after_time_miss
    calls_after_revision_miss = fake.market_calls

    QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision="sha256:" + "b" * 64,
    ).frame(
        sector_id="qmt-gics3:persistent",
        sector_name="Persistent",
        members=members + ("SH.600099",),
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )
    assert fake.market_calls > calls_after_revision_miss


def test_qmt_component_fact_cache_invalidates_on_factor_revision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class RevisableFactorXtdata(FakeXtdata):
        divisor = 1

        def get_divid_factors(self, stock_code, start_time="", end_time=""):
            self.factor_calls.append((stock_code, start_time, end_time))
            return pd.DataFrame(
                [
                    {
                        "interest": 0,
                        "stockBonus": 0,
                        "stockGift": 0,
                        "allotNum": 0,
                        "allotPrice": 0,
                        "gugai": 0,
                        "dr": self.divisor,
                    }
                ],
                index=(AS_OF.date().isoformat(),),
            )

    fake = RevisableFactorXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))
    arguments = {
        "sector_id": "qmt-gics3:factor-revision",
        "sector_name": "Factor Revision",
        "members": members,
        "frequency": "5m",
        "as_of": AS_OF,
        "request_bars": 4,
    }
    options = {
        "minimum_member_count": 8,
        "fact_cache_directory": tmp_path,
        "fact_cache_revision": "sha256:" + "f" * 64,
    }
    first = QmtSectorCompositeSource(**options).frame(**arguments)
    calls_after_first = fake.market_calls

    fake.divisor = 2
    second = QmtSectorCompositeSource(**options).frame(**arguments)

    assert fake.market_calls > calls_after_first
    assert first.attrs["sector_factor_revision"] != second.attrs[
        "sector_factor_revision"
    ]
    assert first.attrs["price_basis_revision"] != second.attrs[
        "price_basis_revision"
    ]


def test_qmt_component_fact_cache_rejects_rehashed_future_bar(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))
    revision = "sha256:" + "c" * 64
    arguments = {
        "sector_id": "qmt-gics3:causal",
        "sector_name": "Causal",
        "members": members,
        "frequency": "5m",
        "as_of": AS_OF,
        "request_bars": 4,
    }
    QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(**arguments)
    [cache_path] = tuple(tmp_path.glob("*.json"))
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    document["payload"]["rows"][-1]["date"] = (
        AS_OF + timedelta(minutes=5)
    ).isoformat()
    document["content_sha256"] = sha256_json(document["payload"])
    cache_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    calls_before_reload = fake.market_calls

    recovered = QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(**arguments)

    assert fake.market_calls > calls_before_reload
    assert not recovered.empty
    assert recovered["date"].iloc[-1].to_pydatetime() == AS_OF


def test_qmt_component_fact_cache_rejects_rehashed_low_member_coverage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))
    revision = "sha256:" + "c" * 64
    arguments = {
        "sector_id": "qmt-gics3:coverage-tamper",
        "sector_name": "Coverage Tamper",
        "members": members,
        "frequency": "5m",
        "as_of": AS_OF,
        "request_bars": 4,
    }
    QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(**arguments)
    [cache_path] = tuple(tmp_path.glob("*.json"))
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    document["payload"]["rows"][0]["volume"] = "7.0"
    document["content_sha256"] = sha256_json(document["payload"])
    cache_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    calls_before_reload = fake.market_calls

    recovered = QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(**arguments)

    assert fake.market_calls > calls_before_reload
    assert not recovered.empty
    assert (recovered["volume"] == 8).all()


def test_qmt_component_fact_cache_rejects_rehashed_member_mask(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))
    revision = "sha256:" + "f" * 64
    arguments = {
        "sector_id": "qmt-gics3:member-mask-tamper",
        "sector_name": "Member Mask Tamper",
        "members": members,
        "frequency": "5m",
        "as_of": AS_OF,
        "request_bars": 4,
    }
    QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(**arguments)
    [cache_path] = tuple(tmp_path.glob("*.json"))
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    document["payload"]["rows"][0]["member_mask"] = (1 << 7) - 1
    document["content_sha256"] = sha256_json(document["payload"])
    cache_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    calls_before_reload = fake.market_calls

    recovered = QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(**arguments)

    assert fake.market_calls > calls_before_reload
    assert not recovered.empty
    assert (recovered["member_mask"] == (1 << 8) - 1).all()


def test_qmt_component_fact_cache_rejects_rehashed_off_grid_bar(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    members = tuple(f"SH.6000{index:02d}" for index in range(8))
    revision = "sha256:" + "d" * 64
    arguments = {
        "sector_id": "qmt-gics3:grid-tamper",
        "sector_name": "Grid Tamper",
        "members": members,
        "frequency": "5m",
        "as_of": AS_OF,
        "request_bars": 4,
    }
    QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(**arguments)
    [cache_path] = tuple(tmp_path.glob("*.json"))
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    original = datetime.fromisoformat(document["payload"]["rows"][0]["date"])
    document["payload"]["rows"][0]["date"] = (
        original + timedelta(minutes=1)
    ).isoformat()
    document["content_sha256"] = sha256_json(document["payload"])
    cache_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    calls_before_reload = fake.market_calls

    recovered = QmtSectorCompositeSource(
        minimum_member_count=8,
        fact_cache_directory=tmp_path,
        fact_cache_revision=revision,
    ).frame(**arguments)

    assert fake.market_calls > calls_before_reload
    assert not recovered.empty
    assert all(value.minute % 5 == 0 for value in recovered["date"])


class DailyFakeXtdata:
    def __init__(
        self,
        *,
        latest_session: date | None = None,
        benchmark_latest_session: date | None = None,
        download_repairs_members: bool = False,
        instrument_details: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.market_calls = 0
        self.latest_session = latest_session or (AS_OF.date() - timedelta(days=1))
        self.benchmark_latest_session = (
            benchmark_latest_session or self.latest_session
        )
        self.download_repairs_members = download_repairs_members
        self.instrument_details = instrument_details or {}
        self.instrument_detail_calls: list[str] = []
        self.download_calls: list[tuple[object, ...]] = []
        self.batch_download_calls: list[tuple[object, ...]] = []
        self.calendar_calls: list[tuple[object, ...]] = []

    @staticmethod
    def _timestamp(value: date) -> int:
        return int(
            datetime.combine(value, datetime.min.time(), tzinfo=SHANGHAI)
            .timestamp()
            * 1000
        )

    def get_trading_dates(self, market, start_time, end_time, count):
        self.calendar_calls.append(("dates", market, start_time, end_time, count))
        assert market == "SH"
        assert count == -1
        start = datetime.strptime(start_time, "%Y%m%d").date()
        end = datetime.strptime(end_time, "%Y%m%d").date()
        return [
            self._timestamp(start + timedelta(days=offset))
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() < 5
            and start + timedelta(days=offset) <= AS_OF.date()
        ]

    def get_market_last_trade_date(self, market):
        self.calendar_calls.append(("last", market))
        assert market == "SH"
        return self._timestamp(AS_OF.date())

    def download_history_data(self, stock_code, period, **kwargs):
        self.download_calls.append((stock_code, period, kwargs))
        return None

    def get_instrument_detail(self, stock_code, iscomplete=False):
        assert iscomplete is False
        self.instrument_detail_calls.append(stock_code)
        return self.instrument_details.get(stock_code)

    def download_history_data2(
        self,
        stock_list,
        period,
        start_time="",
        end_time="",
        callback=None,
        incrementally=None,
    ):
        self.batch_download_calls.append(
            (
                tuple(stock_list),
                period,
                start_time,
                end_time,
                callback,
                incrementally,
            )
        )
        if self.download_repairs_members:
            self.latest_session = AS_OF.date()
            self.benchmark_latest_session = AS_OF.date()
        return None

    def get_market_data(self, **kwargs):
        self.market_calls += 1
        assert kwargs["period"] == "1d"
        assert kwargs["dividend_type"] == "front_ratio"
        codes = tuple(kwargs["stock_list"])
        columns = tuple(range(260))
        fields: dict[str, pd.DataFrame] = {}
        for field in ("time", "open", "high", "low", "close", "volume"):
            rows = []
            for member_index, code in enumerate(codes):
                latest = (
                    self.benchmark_latest_session
                    if code == "000300.SH"
                    else self.latest_session
                )
                dates = tuple(
                    datetime.combine(
                        latest - timedelta(days=offset),
                        datetime.min.time(),
                        tzinfo=SHANGHAI,
                    )
                    for offset in range(259, -1, -1)
                )
                native_times = [int(value.timestamp() * 1000) for value in dates]
                base = Decimal("10") + Decimal(member_index)
                values = []
                for bar_index in range(len(dates)):
                    close = base + Decimal(bar_index) / Decimal("100")
                    values.append(
                        {
                            "time": native_times[bar_index],
                            "open": float(close - Decimal("0.01")),
                            "high": float(close + Decimal("0.02")),
                            "low": float(close - Decimal("0.02")),
                            "close": float(close),
                            "volume": float(1000 + bar_index),
                        }[field]
                    )
                rows.append(values)
            fields[field] = pd.DataFrame(rows, index=codes, columns=columns)
        return fields


def test_daily_strength_normalization_is_future_scale_invariant() -> None:
    known = datetime(2026, 7, 22, 15, 0, tzinfo=SHANGHAI)

    def bars(scale: Decimal):
        return tuple(
            subject.DailyMarketBar(
                session=known.date() + timedelta(days=index),
                open=value * scale,
                high=(value + Decimal("0.2")) * scale,
                low=(value - Decimal("0.2")) * scale,
                close=value * scale,
                volume=Decimal("1000") + index,
                known_at=known + timedelta(days=index),
            )
            for index, value in enumerate((Decimal("10"), Decimal("11")))
        )

    first = subject._normalize_equal_ratio_daily_bars(bars(Decimal("1")))
    future_scaled = subject._normalize_equal_ratio_daily_bars(
        bars(Decimal("0.73456789"))
    )

    assert first == future_scaled
    assert first[-1].close == Decimal("1.000000000000")


def test_daily_strength_evidence_is_stable_under_future_equal_ratio_scale(
    monkeypatch,
) -> None:
    class ScaledDailyXtdata(DailyFakeXtdata):
        def __init__(self, scale: float) -> None:
            super().__init__()
            self.scale = scale

        def get_market_data(self, **kwargs):
            fields = super().get_market_data(**kwargs)
            for field in ("open", "high", "low", "close"):
                fields[field] = fields[field] * self.scale
            return fields

    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    revisions: list[str] = []
    documents: list[dict[str, object]] = []
    for scale in (1.0, 0.73456789):
        monkeypatch.setattr(subject, "xtdata", ScaledDailyXtdata(scale))
        result = QmtSectorStrengthSource().strengths(
            **_daily_strength_arguments()
        )
        revisions.append(result.evidence_revision)
        documents.append(result.evidence_document())

    assert revisions[0] == revisions[1]
    assert documents[0] == documents[1]


def _daily_strength_arguments() -> dict[str, object]:
    return {
        "members_by_sector": {"qmt-gics3:daily": ("SH.600000",)},
        "as_of": AS_OF,
        "membership_revision": "sha256:" + "d" * 64,
    }


class ShortDailyHistoryXtdata(DailyFakeXtdata):
    def __init__(self, *, internal_gap: bool = False) -> None:
        required = AS_OF.date() - timedelta(days=1)
        listed_on = required - timedelta(days=2)
        super().__init__(
            latest_session=required,
            benchmark_latest_session=required,
            instrument_details={
                "600000.SH": {
                    "OpenDate": listed_on.strftime("%Y%m%d"),
                    "InstrumentName": "新上市样本",
                    "TradingDay": AS_OF.date().strftime("%Y%m%d"),
                    "InstrumentStatus": 0,
                    "IsTrading": True,
                }
            },
        )
        self.internal_gap = internal_gap

    def get_market_data(self, **kwargs):
        fields = super().get_market_data(**kwargs)
        for frame in fields.values():
            if "600000.SH" not in frame.index:
                continue
            frame.loc["600000.SH", frame.columns[:-3]] = float("nan")
            if self.internal_gap:
                frame.loc["600000.SH", frame.columns[-2]] = float("nan")
        return fields


def test_qmt_new_listing_requires_open_date_and_complete_session_chain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = ShortDailyHistoryXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    actual_builder = subject.build_horizontal_sector_strength_batch
    builder_calls: list[object] = []

    def recording_builder(**kwargs):
        builder_calls.append(kwargs)
        return actual_builder(**kwargs)

    monkeypatch.setattr(
        subject,
        "build_horizontal_sector_strength_batch",
        recording_builder,
    )
    daily_path = tmp_path / "daily.json"
    instrument_dir = tmp_path / "member-facts"
    revision = "sha256:" + "a" * 64
    source = QmtSectorStrengthSource(
        fact_cache_path=daily_path,
        fact_cache_revision=revision,
        status_fact_directory=instrument_dir,
        status_capture_clock=lambda: AS_OF,
    )

    first = source.strengths(**_daily_strength_arguments())

    [member] = builder_calls[-1]["members_by_sector"]["qmt-gics3:daily"]
    assert member.history_status == "NEW_LISTING"
    assert member.listed_on == AS_OF.date() - timedelta(days=3)
    assert first.evidence_document()["sectors"][0][
        "member_history_statuses"
    ] == [["SH.600000", "NEW_LISTING"]]
    assert fake.instrument_detail_calls == ["600000.SH"]
    [listing_path] = tuple((instrument_dir / "listing").glob("*.json"))
    listing_payload = json.loads(
        listing_path.read_text(encoding="utf-8")
    )["payload"]
    assert listing_payload["facts"]["SH.600000"]["open_date"] == (
        AS_OF.date() - timedelta(days=3)
    ).isoformat()

    # A new source reuses the immutable listing fact and the raw daily facts;
    # it does not need a second instrument-detail query.
    QmtSectorStrengthSource(
        fact_cache_path=daily_path,
        fact_cache_revision=revision,
        status_fact_directory=instrument_dir,
        status_capture_clock=lambda: AS_OF,
    ).strengths(**_daily_strength_arguments())
    assert fake.instrument_detail_calls == ["600000.SH"]


def test_qmt_new_listing_gap_stays_unresolved_and_is_retried(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = ShortDailyHistoryXtdata(internal_gap=True)
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    actual_builder = subject.build_horizontal_sector_strength_batch
    statuses: list[str] = []

    def recording_builder(**kwargs):
        [member] = kwargs["members_by_sector"]["qmt-gics3:daily"]
        statuses.append(member.history_status)
        return actual_builder(**kwargs)

    monkeypatch.setattr(
        subject,
        "build_horizontal_sector_strength_batch",
        recording_builder,
    )
    source = QmtSectorStrengthSource(
        fact_cache_path=tmp_path / "daily.json",
        fact_cache_revision="sha256:" + "b" * 64,
        status_fact_directory=tmp_path / "member-facts",
        status_capture_clock=lambda: AS_OF,
    )

    source.strengths(**_daily_strength_arguments())
    calls_after_first = fake.market_calls
    source.strengths(**_daily_strength_arguments())

    assert statuses == ["UNEXPLAINED_GAP", "UNEXPLAINED_GAP"]
    assert fake.market_calls > calls_after_first


def test_qmt_listing_fact_rejects_rehashed_future_capture(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = ShortDailyHistoryXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    daily_path = tmp_path / "daily.json"
    instrument_dir = tmp_path / "member-facts"
    revision = "sha256:" + "c" * 64
    QmtSectorStrengthSource(
        fact_cache_path=daily_path,
        fact_cache_revision=revision,
        status_fact_directory=instrument_dir,
        status_capture_clock=lambda: AS_OF,
    ).strengths(**_daily_strength_arguments())
    [listing_path] = tuple((instrument_dir / "listing").glob("*.json"))
    forged = json.loads(listing_path.read_text(encoding="utf-8"))
    forged["payload"]["captured_at"] = (
        AS_OF + timedelta(minutes=1)
    ).isoformat()
    forged["content_sha256"] = sha256_json(forged["payload"])
    listing_path.write_text(
        json.dumps(forged, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    fake.instrument_details = {}
    actual_builder = subject.build_horizontal_sector_strength_batch
    statuses: list[str] = []

    def recording_builder(**kwargs):
        [member] = kwargs["members_by_sector"]["qmt-gics3:daily"]
        statuses.append(member.history_status)
        return actual_builder(**kwargs)

    monkeypatch.setattr(
        subject,
        "build_horizontal_sector_strength_batch",
        recording_builder,
    )

    QmtSectorStrengthSource(
        fact_cache_path=daily_path,
        fact_cache_revision=revision,
        status_fact_directory=instrument_dir,
        status_capture_clock=lambda: AS_OF,
    ).strengths(**_daily_strength_arguments())

    assert statuses == ["UNEXPLAINED_GAP"]


def test_qmt_daily_fact_cache_recomputes_strength_with_current_code(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = DailyFakeXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    actual_builder = subject.build_horizontal_sector_strength_batch
    builder_calls: list[object] = []

    def recording_builder(**kwargs):
        builder_calls.append(kwargs)
        return actual_builder(**kwargs)

    monkeypatch.setattr(
        subject,
        "build_horizontal_sector_strength_batch",
        recording_builder,
    )
    path = tmp_path / "daily.json"
    revision = "sha256:" + "e" * 64

    first = QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**_daily_strength_arguments())
    calls_after_first = fake.market_calls
    second = QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**_daily_strength_arguments())

    assert fake.market_calls == calls_after_first == 1
    assert len(builder_calls) == 2
    assert (
        first.evidence_document()["schema"]
        == "chanlun-horizontal-sector-strength-evidence"
    )
    assert second.evidence_document() == first.evidence_document()

    after_close = _daily_strength_arguments()
    # Equality boundary: the 15:00 close itself belongs to the after-close
    # identity and requires the decision session's completed daily bar.
    after_close["as_of"] = AS_OF.replace(hour=15, minute=0)
    after_close_source = QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    )
    stale = after_close_source.strengths(**after_close)
    assert fake.market_calls == calls_after_first + 1
    assert builder_calls[-1]["benchmark_daily"] == ()
    assert all(not value.resolved for value in stale.values())
    stale_document = json.loads(path.read_text(encoding="utf-8"))
    assert stale_document["payload"]["after_daily_close"] is True
    assert stale_document["payload"]["incomplete_symbols"] == [
        "SH.000300",
        "SH.600000",
    ]

    # Once QMT publishes the completed decision-session benchmark bar, the
    # same long-lived source must retry, recover and persist.  An unresolved
    # publication lag must never become an in-memory result for the day.
    fake.latest_session = AS_OF.date()
    fake.benchmark_latest_session = AS_OF.date()
    fresh = after_close_source.strengths(**after_close)
    assert fake.market_calls == calls_after_first + 2
    assert builder_calls[-1]["benchmark_daily"][-1].session == AS_OF.date()
    assert fresh.evidence_document()["decision_time"].startswith(
        AS_OF.date().isoformat()
    )
    fresh_document = json.loads(path.read_text(encoding="utf-8"))
    assert fresh_document["payload"]["after_daily_close"] is True
    assert fresh_document["payload"]["incomplete_symbols"] == []
    assert fresh_document["payload"]["required_daily_session"] == (
        AS_OF.date().isoformat()
    )

    calls_after_fresh = fake.market_calls
    QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**after_close)
    assert fake.market_calls == calls_after_fresh


def test_qmt_daily_fact_cache_rejects_stale_benchmark_before_close(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The benchmark must reach the last completed session even before 15:00."""

    required = AS_OF.date() - timedelta(days=1)
    fake = DailyFakeXtdata(
        latest_session=required,
        benchmark_latest_session=required - timedelta(days=20),
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    path = tmp_path / "daily.json"
    revision = "sha256:" + "3" * 64
    source = QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    )

    stale = source.strengths(**_daily_strength_arguments())

    assert all(not value.resolved for value in stale.values())
    stale_payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
    assert stale_payload["incomplete_symbols"] == ["SH.000300"]
    calls_after_stale = fake.market_calls
    fake.batch_download_calls.clear()

    fake.benchmark_latest_session = required
    recovered = source.strengths(**_daily_strength_arguments())

    assert fake.market_calls > calls_after_stale
    assert fake.batch_download_calls == [
        (("000300.SH",), "1d", "", "", None, True)
    ]
    assert tuple(recovered.values())
    persisted = json.loads(path.read_text(encoding="utf-8"))["payload"]
    assert persisted["required_daily_session"] == required.isoformat()
    assert persisted["incomplete_symbols"] == []
    assert persisted["bars"]["SH.000300"][-1][0] == required.isoformat()


def test_qmt_daily_source_batch_refreshes_benchmark_and_members(
    monkeypatch,
    tmp_path: Path,
) -> None:
    required = AS_OF.date() - timedelta(days=1)
    fake = DailyFakeXtdata(
        latest_session=required,
        benchmark_latest_session=required - timedelta(days=20),
        download_repairs_members=True,
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    path = tmp_path / "daily.json"
    result = QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision="sha256:" + "4" * 64,
    ).strengths(**_daily_strength_arguments())

    assert fake.download_calls == []
    assert fake.batch_download_calls == [
        (
            ("000300.SH", "600000.SH"),
            "1d",
            "",
            "",
            None,
            True,
        )
    ]
    assert tuple(result.values())
    persisted = json.loads(path.read_text(encoding="utf-8"))["payload"]
    assert persisted["required_daily_session"] == required.isoformat()
    assert persisted["incomplete_symbols"] == []
    assert persisted["bars"]["SH.000300"][-1][0] == required.isoformat()


def test_qmt_daily_fact_cache_rejects_stale_member_before_close(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A current benchmark must not make a stale member history complete."""

    required = AS_OF.date() - timedelta(days=1)
    fake = DailyFakeXtdata(
        latest_session=required - timedelta(days=20),
        benchmark_latest_session=required,
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    actual_builder = subject.build_horizontal_sector_strength_batch
    builder_calls: list[object] = []

    def recording_builder(**kwargs):
        builder_calls.append(kwargs)
        return actual_builder(**kwargs)

    monkeypatch.setattr(
        subject,
        "build_horizontal_sector_strength_batch",
        recording_builder,
    )
    path = tmp_path / "daily.json"
    source = QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision="sha256:" + "5" * 64,
    )

    stale = source.strengths(**_daily_strength_arguments())

    assert tuple(stale.values())
    [stale_member] = builder_calls[-1]["members_by_sector"]["qmt-gics3:daily"]
    assert stale_member.history_status == "UNEXPLAINED_GAP"
    stale_payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
    assert stale_payload["incomplete_symbols"] == ["SH.600000"]
    calls_after_stale = fake.market_calls
    fake.batch_download_calls.clear()

    # The same singleton retries rather than freezing the transient gap.
    fake.latest_session = required
    recovered = source.strengths(**_daily_strength_arguments())

    assert fake.market_calls > calls_after_stale
    assert fake.batch_download_calls == [
        (("600000.SH",), "1d", "", "", None, True)
    ]
    assert tuple(recovered.values())
    [recovered_member] = builder_calls[-1]["members_by_sector"][
        "qmt-gics3:daily"
    ]
    assert recovered_member.history_status == "COMPLETE"
    persisted = json.loads(path.read_text(encoding="utf-8"))["payload"]
    assert persisted["incomplete_symbols"] == []
    assert persisted["bars"]["SH.600000"][-1][0] == required.isoformat()

    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["payload"]["bars"]["SH.600000"].pop()
    forged["content_sha256"] = sha256_json(forged["payload"])
    path.write_text(
        json.dumps(forged, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    calls_before_repair = fake.market_calls

    QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision="sha256:" + "5" * 64,
    ).strengths(**_daily_strength_arguments())

    assert fake.market_calls > calls_before_repair
    repaired = json.loads(path.read_text(encoding="utf-8"))["payload"]
    assert repaired["bars"]["SH.600000"][-1][0] == required.isoformat()


def test_qmt_same_session_suspension_fact_resolves_and_replays_forward(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A captured suspension may explain a gap only at/after its capture."""

    session = AS_OF.date()
    decision = AS_OF.replace(hour=15, minute=5)
    fake = DailyFakeXtdata(
        latest_session=session - timedelta(days=1),
        benchmark_latest_session=session,
        instrument_details={
            "600000.SH": {
                "TradingDay": session.strftime("%Y%m%d"),
                "InstrumentName": "停牌样本",
                "InstrumentStatus": 2,
                "IsTrading": False,
            }
        },
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    actual_builder = subject.build_horizontal_sector_strength_batch
    builder_calls: list[object] = []

    def recording_builder(**kwargs):
        builder_calls.append(kwargs)
        return actual_builder(**kwargs)

    monkeypatch.setattr(
        subject,
        "build_horizontal_sector_strength_batch",
        recording_builder,
    )
    daily_path = tmp_path / "daily.json"
    status_dir = tmp_path / "member-status"
    revision = "sha256:" + "6" * 64
    arguments = _daily_strength_arguments()
    arguments["as_of"] = decision

    result = QmtSectorStrengthSource(
        fact_cache_path=daily_path,
        fact_cache_revision=revision,
        status_fact_directory=status_dir,
        status_capture_clock=lambda: decision,
    ).strengths(**arguments)

    first_evidence = result.evidence_document()["sectors"][0]
    assert first_evidence["usable_member_count"] == 1
    assert first_evidence["member_history_statuses"] == [
        ["SH.600000", "SUSPENDED"]
    ]
    [member] = builder_calls[-1]["members_by_sector"]["qmt-gics3:daily"]
    assert member.history_status == "SUSPENDED"
    assert fake.instrument_detail_calls == ["600000.SH"]
    [status_path] = tuple((status_dir / session.isoformat()).glob("*.json"))
    status_payload = json.loads(status_path.read_text(encoding="utf-8"))["payload"]
    assert status_payload["captured_at"] == decision.isoformat()
    assert status_payload["facts"]["SH.600000"]["instrument_status"] == 2
    assert status_payload["tick_data_used"] is False

    # The next morning cannot query historical suspension from this QMT
    # client, but it may consume yesterday's immutable same-session capture.
    next_morning = (decision + timedelta(days=1)).replace(hour=10, minute=30)
    forward = dict(arguments)
    forward["as_of"] = next_morning
    replayed = QmtSectorStrengthSource(
        fact_cache_path=daily_path,
        fact_cache_revision=revision,
        status_fact_directory=status_dir,
        status_capture_clock=lambda: next_morning,
    ).strengths(**forward)

    assert replayed.evidence_document()["sectors"][0]["usable_member_count"] == 1
    [forward_member] = builder_calls[-1]["members_by_sector"][
        "qmt-gics3:daily"
    ]
    assert forward_member.history_status == "SUSPENDED"
    assert fake.instrument_detail_calls == ["600000.SH"]

    # Re-hashing a forged normal status must not turn it into suspension
    # evidence.  The semantic validator rejects it and the sector closes.
    forged = json.loads(status_path.read_text(encoding="utf-8"))
    forged["payload"]["facts"]["SH.600000"]["instrument_status"] = 0
    forged["content_sha256"] = sha256_json(forged["payload"])
    status_path.write_text(
        json.dumps(forged, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    rejected = QmtSectorStrengthSource(
        fact_cache_path=daily_path,
        fact_cache_revision=revision,
        status_fact_directory=status_dir,
        status_capture_clock=lambda: next_morning,
    ).strengths(**forward)
    assert rejected.evidence_document()["sectors"][0]["usable_member_count"] == 0


def test_qmt_suspension_fact_never_backfills_before_capture(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = AS_OF.date()
    captured_at = AS_OF.replace(hour=15, minute=5)
    decision = AS_OF.replace(hour=15, minute=0)
    fake = DailyFakeXtdata(
        latest_session=session - timedelta(days=1),
        benchmark_latest_session=session,
        instrument_details={
            "600000.SH": {
                "TradingDay": session.strftime("%Y%m%d"),
                "InstrumentName": "停牌样本",
                "InstrumentStatus": 5,
                "IsTrading": False,
            }
        },
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    status_dir = tmp_path / "member-status"
    revision = "sha256:" + "7" * 64

    # First produce an authentic fact at 15:05.
    after = _daily_strength_arguments()
    after["as_of"] = captured_at
    QmtSectorStrengthSource(
        fact_cache_path=tmp_path / "daily-after.json",
        fact_cache_revision=revision,
        status_fact_directory=status_dir,
        status_capture_clock=lambda: captured_at,
    ).strengths(**after)

    # Replaying 15:00 later must neither load the 15:05 fact nor label a new
    # native read as if it had been known five minutes earlier.
    before = dict(after)
    before["as_of"] = decision
    result = QmtSectorStrengthSource(
        fact_cache_path=tmp_path / "daily-before.json",
        fact_cache_revision=revision,
        status_fact_directory=status_dir,
        status_capture_clock=lambda: captured_at + timedelta(minutes=1),
    ).strengths(**before)

    assert result.evidence_document()["sectors"][0]["usable_member_count"] == 0


@pytest.mark.parametrize("instrument_status", (0, -1))
def test_qmt_nonpositive_status_does_not_explain_missing_bar(
    monkeypatch,
    tmp_path: Path,
    instrument_status: int,
) -> None:
    session = AS_OF.date()
    decision = AS_OF.replace(hour=15, minute=5)
    fake = DailyFakeXtdata(
        latest_session=session - timedelta(days=1),
        benchmark_latest_session=session,
        instrument_details={
            "600000.SH": {
                "TradingDay": session.strftime("%Y%m%d"),
                "InstrumentName": "状态未决样本",
                "InstrumentStatus": instrument_status,
                "IsTrading": False,
            }
        },
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    arguments = _daily_strength_arguments()
    arguments["as_of"] = decision
    status_dir = tmp_path / "member-status"

    result = QmtSectorStrengthSource(
        fact_cache_path=tmp_path / "daily.json",
        fact_cache_revision="sha256:" + "8" * 64,
        status_fact_directory=status_dir,
        status_capture_clock=lambda: decision,
    ).strengths(**arguments)

    assert result.evidence_document()["sectors"][0]["usable_member_count"] == 0
    assert not status_dir.exists()


def test_qmt_daily_fact_cache_fails_closed_on_identity_and_causality(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = DailyFakeXtdata()
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    path = tmp_path / "daily.json"
    revision = "sha256:" + "f" * 64
    arguments = _daily_strength_arguments()
    QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**arguments)

    # A foreign producer cannot reuse the facts.
    calls_before_foreign = fake.market_calls
    QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision="sha256:" + "0" * 64,
    ).strengths(**arguments)
    assert fake.market_calls > calls_before_foreign

    # Recreate a same-producer document, then forge a future known_at and
    # recompute its content hash.  Semantic validation must still reject it.
    QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**arguments)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["bars"]["SH.600000"][-1][6] = (
        AS_OF + timedelta(days=1)
    ).isoformat()
    document["content_sha256"] = sha256_json(document["payload"])
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    calls_before_future = fake.market_calls
    QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**arguments)
    assert fake.market_calls > calls_before_future

    # The exact requested symbol set is also part of the fact identity.
    calls_before_symbols = fake.market_calls
    changed = dict(arguments)
    changed["members_by_sector"] = {
        "qmt-gics3:daily": ("SH.600000", "SH.600001")
    }
    QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**changed)
    assert fake.market_calls > calls_before_symbols


def test_qmt_daily_fact_cache_rejects_rehashed_stale_after_close_bundle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = DailyFakeXtdata(latest_session=AS_OF.date())
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    path = tmp_path / "daily.json"
    revision = "sha256:" + "2" * 64
    arguments = _daily_strength_arguments()
    arguments["as_of"] = AS_OF.replace(hour=15, minute=0)
    QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**arguments)

    forged = json.loads(path.read_text(encoding="utf-8"))
    assert forged["payload"]["after_daily_close"] is True
    forged["payload"]["bars"]["SH.000300"].pop()
    forged["content_sha256"] = sha256_json(forged["payload"])
    path.write_text(
        json.dumps(forged, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    calls_before_recovery = fake.market_calls

    recovered = QmtSectorStrengthSource(
        fact_cache_path=path,
        fact_cache_revision=revision,
    ).strengths(**arguments)

    assert fake.market_calls > calls_before_recovery
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["payload"]["bars"]["SH.000300"][-1][0] == (
        AS_OF.date().isoformat()
    )
    assert recovered.evidence_document()["decision_time"].startswith(
        AS_OF.date().isoformat()
    )


class _CalendarXtdata:
    def __init__(
        self,
        *,
        returned: tuple[date, ...],
        published_through: date,
    ) -> None:
        self.returned = returned
        self.published_through = published_through
        self.calls: list[tuple[object, ...]] = []

    @staticmethod
    def _timestamp(value: date) -> int:
        return int(
            datetime.combine(value, datetime.min.time(), tzinfo=SHANGHAI)
            .timestamp()
            * 1000
        )

    def get_trading_dates(self, market, start_time, end_time, count):
        self.calls.append(("dates", market, start_time, end_time, count))
        return [self._timestamp(value) for value in self.returned]

    def get_market_last_trade_date(self, market):
        self.calls.append(("last", market))
        return self._timestamp(self.published_through)


def test_qmt_calendar_adapter_proves_historical_weekday_holiday(
    monkeypatch,
) -> None:
    holiday = date(2026, 7, 29)
    fake = _CalendarXtdata(
        returned=(),
        published_through=date(2026, 7, 30),
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    evidence = subject.qmt_trading_session_evidence(
        session=holiday,
        observed_at=datetime(2026, 7, 30, 16, tzinfo=SHANGHAI),
    )

    assert evidence["classification"] == "NON_TRADING_SESSION"
    assert evidence["reason_code"] == "QMT_NON_TRADING_SESSION_CONFIRMED"
    assert evidence["tick_data_used"] is False
    assert evidence["real_account_accessed"] is False
    assert evidence["real_order_transport_enabled"] is False
    assert fake.calls == [
        ("dates", "SH", "20260729", "20260729", -1),
        ("last", "SH"),
    ]


def test_qmt_calendar_adapter_does_not_guess_unpublished_current_day(
    monkeypatch,
) -> None:
    current = date(2026, 7, 31)
    fake = _CalendarXtdata(
        returned=(),
        published_through=date(2026, 7, 30),
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    evidence = subject.qmt_trading_session_evidence(
        session=current,
        observed_at=datetime(2026, 7, 31, 1, tzinfo=SHANGHAI),
    )

    assert evidence["classification"] == "UNRESOLVED"
    assert evidence["reason_code"] == (
        "QMT_TRADING_CALENDAR_NOT_PUBLISHED_THROUGH_SESSION"
    )


def test_qmt_calendar_adapter_converts_native_failure_to_unresolved(
    monkeypatch,
) -> None:
    class _Broken:
        @staticmethod
        def get_trading_dates(*_args):
            raise RuntimeError("native calendar unavailable")

    monkeypatch.setattr(subject, "xtdata", _Broken())
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    evidence = subject.qmt_trading_session_evidence(
        session=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 16, tzinfo=SHANGHAI),
    )

    assert evidence["classification"] == "UNRESOLVED"
    assert evidence["reason_code"] == "QMT_TRADING_CALENDAR_UNAVAILABLE"
    assert evidence["query_attempted"] is True
    assert evidence["query_succeeded"] is False


def test_qmt_bulk_calendar_returns_only_the_published_exact_interval(
    monkeypatch,
) -> None:
    sessions = (date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 30))
    fake = _CalendarXtdata(
        returned=sessions,
        published_through=date(2026, 7, 30),
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    result = subject.qmt_trading_sessions(
        start=date(2026, 7, 27),
        end=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 16, tzinfo=SHANGHAI),
    )

    assert result == sessions
    assert fake.calls == [
        ("dates", "SH", "20260727", "20260730", -1),
        ("last", "SH"),
    ]


def test_qmt_bulk_calendar_rejects_an_unpublished_interval_tail(
    monkeypatch,
) -> None:
    fake = _CalendarXtdata(
        returned=(date(2026, 7, 29),),
        published_through=date(2026, 7, 29),
    )
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())

    with pytest.raises(RuntimeError, match="not published through"):
        subject.qmt_trading_sessions(
            start=date(2026, 7, 29),
            end=date(2026, 7, 30),
            observed_at=datetime(2026, 7, 30, 16, tzinfo=SHANGHAI),
        )
