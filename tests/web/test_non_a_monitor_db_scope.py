import types

from cl_app import create_app
from cl_app.services.holding_group_monitor import (
    HoldingGroupMonitorConfig,
    HoldingGroupMonitorService,
)
from chanlun.persistence.db import db


def _app():
    return create_app(
        start_scheduler=False,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": False,
            "HOLDING_GROUP_MONITOR_MAX_SYMBOLS": 12,
            "TRADING_SCREENING_MANUAL_HOLDING_GROUP": "我的持仓",
        },
    )


def _row(market: str, code: str):
    return types.SimpleNamespace(
        market=market,
        stock_code=code,
        stock_name=code,
    )


def test_non_a_provider_reads_only_configured_groups_with_bounded_queries(
    monkeypatch,
):
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_PRIORITY_WATCHLIST_GROUPS",
        raising=False,
    )
    app = _app()
    calls = []
    rows = {
        "我的持仓": (
            _row("hk", "HK.00700"),
            _row("us", "QCOM.US"),
            *(_row("a", f"SH.{600000 + index:06d}") for index in range(3_000)),
        ),
        "我的关注": tuple(
            _row("us", f"WATCH{index:04d}.US") for index in range(3_000)
        ),
        "旧版结果": tuple(
            _row("us", f"LEGACY{index:04d}.US") for index in range(3_000)
        ),
        "未配置组": tuple(
            _row("us", f"OTHER{index:04d}.US") for index in range(3_000)
        ),
    }

    def bounded_rows(group_name, *, limit=None, markets=None):
        calls.append((group_name, limit, markets))
        assert type(limit) is int and 0 < limit <= 13
        selected = rows[group_name]
        if markets is not None:
            selected = tuple(row for row in selected if row.market in markets)
        return selected[:limit]

    monkeypatch.setattr(
        db,
        "zx_get_global_groups",
        lambda: (_ for _ in ()).throw(
            AssertionError("bounded monitor must not enumerate global groups")
        ),
    )
    monkeypatch.setattr(db, "zx_get_global_group_stocks", bounded_rows)
    try:
        provider = app.extensions["holding_group_monitor"]._positions_provider
        positions = provider()

        assert calls == [
            (
                "我的持仓",
                13,
                (
                    "hk",
                    "futures",
                    "ny_futures",
                    "currency",
                    "currency_spot",
                    "us",
                    "fx",
                ),
            ),
            ("我的关注", 10, ("us",)),
        ]
        assert len(positions) == 12
        assert sum(row["is_holding"] is True for row in positions) == 2
        assert {group for row in positions for group in row["groups"]} == {
            "我的持仓",
            "我的关注",
        }
        assert not any(
            row["code"].startswith(("LEGACY", "OTHER")) for row in positions
        )
    finally:
        app.extensions["shutdown_runtime_services"]()


def test_non_a_holding_overflow_fails_before_exchange_access(monkeypatch, tmp_path):
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_PRIORITY_WATCHLIST_GROUPS",
        raising=False,
    )
    app = _app()
    calls = []
    holdings = tuple(
        _row("us", f"HOLD{index:04d}.US") for index in range(3_000)
    )

    def bounded_rows(group_name, *, limit=None, markets=None):
        calls.append((group_name, limit, markets))
        assert group_name == "我的持仓"
        assert type(limit) is int and 0 < limit <= 13
        return holdings[:limit]

    monkeypatch.setattr(
        db,
        "zx_get_global_groups",
        lambda: (_ for _ in ()).throw(
            AssertionError("bounded monitor must not enumerate global groups")
        ),
    )
    monkeypatch.setattr(db, "zx_get_global_group_stocks", bounded_rows)
    exchange_calls = []

    def forbidden_collector(*_args, **_kwargs):
        raise AssertionError("structure access must follow successful admission")

    try:
        provider = app.extensions["holding_group_monitor"]._positions_provider
        service = HoldingGroupMonitorService(
            positions_provider=provider,
            notifier=None,
            state_root=tmp_path,
            config=HoldingGroupMonitorConfig(max_symbols=12),
            exchange_provider=lambda market: exchange_calls.append(market),
            event_collector=forbidden_collector,
        )

        result = service.run_once()

        assert result["status"] == "error"
        assert result["reason_code"] == "HOLDING_MONITOR_SCOPE_EXCEEDED"
        assert result["requested_count"] == 13
        assert result["mandatory_count"] == 13
        assert calls == [
            (
                "我的持仓",
                13,
                (
                    "hk",
                    "futures",
                    "ny_futures",
                    "currency",
                    "currency_spot",
                    "us",
                    "fx",
                ),
            )
        ]
        assert exchange_calls == []
    finally:
        app.extensions["shutdown_runtime_services"]()
