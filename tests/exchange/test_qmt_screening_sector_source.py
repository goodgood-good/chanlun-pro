from __future__ import annotations

from datetime import datetime, timedelta
from threading import RLock
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.exchange import qmt_screening_sector_source as subject
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_GICS3_CATALOG_SOURCE,
    QMT_GICS3_COMPOSITE_PROVIDER,
    QmtSectorCompositeSource,
    build_qmt_gics3_sector_catalog,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 23, 10, 30, tzinfo=SHANGHAI)


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
        self.market_end = market_end
        self.latest_probe_end = latest_probe_end or market_end
        self.members = {
            "GICS3商业银行": [
                "600000.SH",
                "000001.SZ",
                "430047.BJ",
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

    def get_trading_dates(self, market, start_time, end_time, count):
        assert market == "SH"
        assert start_time < end_time
        assert count == -1
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

    catalog = build_qmt_gics3_sector_catalog()

    assert catalog["source"] == QMT_GICS3_CATALOG_SOURCE
    assert len(catalog["sectors"]) == 1
    [sector] = catalog["sectors"]
    assert sector["source_key"] == "GICS3商业银行"
    assert sector["name"] == "商业银行"
    assert sector["sector_id"].startswith("qmt-gics3:")
    assert sector["member_codes"] == ["BJ.430047", "SH.600000", "SZ.000001"]


def test_qmt_component_source_builds_and_caches_auditable_sector_frame(
    monkeypatch,
) -> None:
    fake = FakeXtdata(latest_probe_end=AS_OF - timedelta(days=1))
    monkeypatch.setattr(subject, "xtdata", fake)
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", RLock())
    source = QmtSectorCompositeSource(minimum_member_count=8)
    members = tuple(f"SH.6000{index:02d}" for index in range(8))

    first = source.frame(
        sector_id="qmt-gics3:test",
        sector_name="测试行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )
    second = source.frame(
        sector_id="qmt-gics3:test",
        sector_name="测试行业",
        members=members,
        frequency="5m",
        as_of=AS_OF,
        request_bars=4,
    )

    assert fake.market_calls == 2
    assert len(fake.download_calls) == len(members)
    assert fake.download_calls[0]["period"] == "5m"
    assert fake.download_calls[0]["incrementally"] is True
    assert len(first) == 4
    assert first.equals(second)
    assert first["date"].is_monotonic_increasing
    assert (first["high"] >= first[["open", "close"]].max(axis=1)).all()
    assert (first["low"] <= first[["open", "close"]].min(axis=1)).all()
    assert first.attrs["price_basis_provider"] == QMT_GICS3_COMPOSITE_PROVIDER
    assert first.attrs["price_basis_adjustment"] == (
        "none-stable-24-member-median-v2"
    )
    assert first.attrs["structure_price_quantum"] == "0.000001"
    assert first.attrs["price_basis_revision"].startswith("sha256:")


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
