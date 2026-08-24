import io

from cl_app import create_app
from cl_app.blueprints import zixuan as zixuan_blueprint


def _app():
    return create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )


def test_blank_group_name_is_rejected_before_zixuan_is_created(monkeypatch):
    created = []
    monkeypatch.setattr(
        zixuan_blueprint,
        "ZiXuan",
        lambda market: created.append(market),
    )
    app = _app()

    response = app.test_client().post(
        "/opt_zixuan_group/a",
        data={"opt": "ADD", "zx_group": "   "},
    )

    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "msg": "无效的自选组名"}
    assert created == []


def test_add_group_returns_actionable_success_contract(monkeypatch):
    calls = []

    class _ZiXuan:
        def __init__(self, market):
            calls.append(("init", market))

        def add_zx_group(self, name):
            calls.append(("add", name))
            return True

    monkeypatch.setattr(zixuan_blueprint, "ZiXuan", _ZiXuan)
    app = _app()

    response = app.test_client().post(
        "/opt_zixuan_group/a",
        data={"opt": "ADD", "zx_group": "  趋势启动  "},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "group": "趋势启动",
        "msg": "自选分组已创建",
    }
    assert calls == [("init", "a"), ("add", "趋势启动")]


def test_duplicate_group_returns_actionable_failure_contract(monkeypatch):
    class _ZiXuan:
        def __init__(self, _market):
            pass

        def add_zx_group(self, _name):
            return False

    monkeypatch.setattr(zixuan_blueprint, "ZiXuan", _ZiXuan)
    app = _app()

    response = app.test_client().post(
        "/opt_zixuan_group/a",
        data={"opt": "ADD", "zx_group": "趋势启动"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": False,
        "group": "趋势启动",
        "msg": "分组已存在或名称不可用",
    }


def test_import_resolves_codes_once_and_replaces_group_atomically(monkeypatch):
    replaced = []
    identity_calls = []

    class _ZiXuan:
        zixuan_list = [{"name": "关注"}]

        def __init__(self, _market):
            pass

        def zx_stocks(self, _group):
            return [
                {
                    "code": "SH.601398",
                    "name": "工商银行",
                    "color": "#ff5722",
                    "memo": "keep",
                }
            ]

        def replace_zx_stocks(self, group, stocks):
            replaced.append((group, stocks))
            return True

        def add_stock(self, *_args, **_kwargs):
            raise AssertionError("import must not commit one row at a time")

    class _Exchange:
        stock_info_query_scope = "SINGLE_SYMBOL_STOCK_INFO"

        def all_stocks(self):
            raise AssertionError("bounded import must never enumerate a market")

        def stock_info(self, code):
            identity_calls.append(code)
            return {
                "SH.600000": {"code": code, "name": "浦发银行"},
                "SZ.000001": {"code": code, "name": "平安银行"},
            }.get(code)

    monkeypatch.setattr(zixuan_blueprint, "ZiXuan", _ZiXuan)
    monkeypatch.setattr(
        zixuan_blueprint, "get_exchange", lambda _market: _Exchange()
    )
    app = _app()

    response = app.test_client().post(
        "/zixuan_opt_import",
        data={
            "market": "a",
            "zx_group": "关注",
            "file": (
                io.BytesIO("600000,浦发\n000001,平安\n".encode("utf-8")),
                "stocks.txt",
            ),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "msg": "成功导入 2 条记录"}
    assert identity_calls == ["SH.600000", "SZ.000001"]
    assert replaced == [
        (
            "关注",
            [
                {
                    "code": "SH.601398",
                    "name": "工商银行",
                    "color": "#ff5722",
                    "memo": "keep",
                },
                {"code": "SH.600000", "name": "浦发"},
                {"code": "SZ.000001", "name": "平安"},
            ],
        )
    ]


def test_import_over_twenty_codes_fails_before_exchange_access(monkeypatch):
    exchange_calls = []
    monkeypatch.setattr(
        zixuan_blueprint,
        "get_exchange",
        lambda market: exchange_calls.append(market),
    )
    app = _app()
    content = "".join(
        f"SH.{600000 + index:06d},name-{index}\n" for index in range(21)
    )

    response = app.test_client().post(
        "/zixuan_opt_import",
        data={
            "market": "a",
            "zx_group": "关注",
            "file": (io.BytesIO(content.encode("utf-8")), "stocks.txt"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "msg": "单次导入最多允许 20 个显式代码",
    }
    assert exchange_calls == []


def test_non_a_import_never_calls_catalog_expanding_stock_info(monkeypatch):
    calls = {"stock_info": 0, "all_stocks": 0, "basicinfo": 0}
    replaced = []

    class _ZiXuan:
        zixuan_list = [{"name": "关注"}]

        def __init__(self, _market):
            pass

        def zx_stocks(self, _group):
            return []

        def replace_zx_stocks(self, group, stocks):
            replaced.append((group, stocks))
            return True

    class _CatalogExpandingExchange:
        def all_stocks(self):
            calls["all_stocks"] += 1
            return [{"code": "HK.00700", "name": "Tencent"}]

        def stock_info(self, code):
            calls["stock_info"] += 1
            calls["basicinfo"] += 1
            return next(
                row for row in self.all_stocks() if row["code"] == code
            )

    monkeypatch.setattr(zixuan_blueprint, "ZiXuan", _ZiXuan)
    monkeypatch.setattr(
        zixuan_blueprint,
        "get_exchange",
        lambda _market: _CatalogExpandingExchange(),
    )
    app = _app()

    response = app.test_client().post(
        "/zixuan_opt_import",
        data={
            "market": "hk",
            "zx_group": "关注",
            "file": (
                io.BytesIO("HK.00700,腾讯控股\n".encode("utf-8")),
                "stocks.txt",
            ),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "msg": "成功导入 1 条记录"}
    assert replaced == [("关注", [{"code": "HK.00700", "name": "腾讯控股"}])]
    assert calls == {"stock_info": 0, "all_stocks": 0, "basicinfo": 0}


def test_a_share_holding_membership_edit_wakes_both_live_lanes(monkeypatch):
    class _ZiXuan:
        def __init__(self, _market):
            pass

        def add_stock(self, _group, _code, _name):
            return True

    class _Screening:
        def __init__(self):
            self.calls = 0

        def notify_instrument_scope_changed(self):
            self.calls += 1

    class _Monitor:
        def __init__(self):
            self.calls = 0

        def request_refresh(self):
            self.calls += 1

    monkeypatch.setattr(zixuan_blueprint, "ZiXuan", _ZiXuan)
    app = _app()
    screening = _Screening()
    monitor = _Monitor()
    app.extensions["decision_support_trading_screening"] = screening
    app.extensions["holding_group_monitor"] = monitor

    response = app.test_client().post(
        "/set_stock_zixuan",
        data={
            "market": "a",
            "opt": "ADD",
            "group_name": "我的持仓",
            "code": "SZ.300826",
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert screening.calls == 1
    assert monitor.calls == 1
