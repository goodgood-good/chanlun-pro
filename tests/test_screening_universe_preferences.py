"""Named scope and name exclusions must apply to the actual merged universe."""

from unittest.mock import Mock

import pytest

from chanlun.screening import markets, runner
from cl_app.services.screening import validate_settings


def catalog(
    monkeypatch, tmp_path, *, scope="all_a_watchlist", exclude=True, groups=None
):
    exchange = Mock()
    exchange.all_stocks.return_value = [
        {"code": "SH.600088", "name": "正常股票", "type": "stock_cn"},
        {"code": "SZ.000001", "name": "*ST样本", "type": "stock_cn"},
    ]
    exchange.stock_info.side_effect = lambda code: {
        "code": code,
        "name": "*ST样本" if code == "SZ.000001" else "正常股票",
    }
    monkeypatch.setattr(runner, "_exchange", lambda: exchange)
    selected_groups = []

    def watch(names=None):
        selected_groups.append(names)
        return [
            {
                "market": "a",
                "code": "SZ.000001",
                "name": "人工别名",
                "origin": "watchlist",
            },
            {
                "market": "us",
                "code": "ST.US",
                "name": "ST Company",
                "origin": "watchlist",
            },
        ]

    monkeypatch.setattr(markets, "watchlist_symbols", watch)
    monkeypatch.setattr(
        markets, "holding_symbols", lambda: [{"market": "a", "code": "SZ.000001"}]
    )
    connection = Mock()
    settings = {
        "scope": scope,
        "exclude_st": exclude,
        **({"watch_groups": groups} if groups else {}),
    }
    runner._catalog_worker(connection, settings, tmp_path)
    return connection.send.call_args.args[0], exchange, selected_groups


def test_watchlist_alias_cannot_reintroduce_a_stock_excluded_by_its_official_name(
    monkeypatch, tmp_path
):
    value, exchange, _ = catalog(monkeypatch, tmp_path)
    assert "error" not in value
    assert {stock["code"] for stock in value["stocks"]} == {"SH.600088", "ST.US"}
    assert value["universe_exclusions"] == [
        {
            "market": "a",
            "code": "SZ.000001",
            "name": "*ST样本",
            "reason": "ST_NAME_EXCLUDED",
        }
    ]
    exchange.stock_info.assert_not_called()


def test_a_watchlist_only_scan_checks_the_official_name_and_selected_groups(
    monkeypatch, tmp_path
):
    value, exchange, groups = catalog(
        monkeypatch, tmp_path, scope="watchlist", groups=["观察"]
    )
    assert [stock["code"] for stock in value["stocks"]] == ["ST.US"]
    assert groups == [["观察"]]
    exchange.all_stocks.assert_not_called()
    exchange.stock_info.assert_called_once_with("SZ.000001")


def test_name_preference_can_be_disabled_without_affecting_the_group_union(
    monkeypatch, tmp_path
):
    value, _, _ = catalog(monkeypatch, tmp_path, exclude=False)
    assert len(value["stocks"]) == 3 and value["universe_exclusions"] == []
    assert (
        next(stock for stock in value["stocks"] if stock["code"] == "SZ.000001")[
            "holding"
        ]
        is True
    )


@pytest.mark.parametrize(
    "body",
    [
        {"scope": "all_a", "watch_groups": ["观察"]},
        {"scope": "watchlist", "watch_groups": []},
        {"scope": "watchlist", "watch_groups": "观察"},
        {"scope": "watchlist", "watch_groups": [" "]},
        {"scope": "watchlist", "watch_groups": [None]},
    ],
)
def test_invalid_group_scope_is_rejected_before_work_starts(body):
    with pytest.raises(ValueError):
        validate_settings(body)


def test_group_scope_is_explicit_and_deduplicated():
    settings = validate_settings(
        {"scope": "watchlist", "watch_groups": ["观察", "持仓", "观察"]}
    )
    assert settings["watch_groups"] == ["观察", "持仓"]
    assert "watch_groups" not in validate_settings({"scope": "watchlist"})


def test_named_watch_groups_do_not_read_other_groups(monkeypatch):
    watch = Mock(zixuan_list=[{"name": "观察"}, {"name": "持仓"}])
    watch.zx_stocks.return_value = [
        {"market": "us", "code": "AAPL.US", "name": "Apple"}
    ]
    monkeypatch.setattr("chanlun.zixuan.ZiXuan", lambda _: watch)
    assert markets.watchlist_symbols(["观察"])[0]["code"] == "AAPL.US"
    watch.zx_stocks.assert_called_once_with("观察")
    with pytest.raises(ValueError, match="不存在"):
        markets.watchlist_symbols(["已删除"])


def test_missing_name_metadata_fails_before_fetching_history(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner,
        "scan_symbol",
        lambda *_a, **_k: pytest.fail(
            "unknown name cannot bypass an enabled name preference"
        ),
    )
    result = runner._scan_worker_symbol_with_capacity(
        {
            "market": "a",
            "code": "SH.600088",
            "name": "alias",
            "_screening_name_missing": True,
        },
        {"frequencies": ["5m"]},
        {},
        tmp_path,
        "revision",
        {},
    )
    assert result["reasons"] == ["SYMBOL_METADATA_UNAVAILABLE"]
    assert result["requested_frequencies"] == ["5m"]


@pytest.mark.parametrize('failure', [None, RuntimeError('catalog unavailable'), {'code':'SH.600001','name':'wrong'}])
def test_requested_symbol_with_missing_or_wrong_metadata_is_reported_not_silently_dropped(monkeypatch, tmp_path, failure):
    exchange = Mock()
    def info(code):
        if code == 'SH.600088':
            return {'code':code,'name':'正常股票'}
        if isinstance(failure, Exception):
            raise failure
        return failure
    exchange.stock_info.side_effect = info
    monkeypatch.setattr(runner, '_exchange', lambda: exchange)
    monkeypatch.setattr(markets, 'holding_symbols', lambda: [])
    connection = Mock()
    runner._catalog_worker(connection, {'scope':'codes','codes':['SH.600088','SZ.301004'],'exclude_st':True}, tmp_path)
    payload = connection.send.call_args.args[0]
    assert [stock['code'] for stock in payload['stocks']] == ['SH.600088','SZ.301004']
    assert payload['stocks'][1]['_screening_name_missing'] is True
