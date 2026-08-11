from types import SimpleNamespace

import chanlun.zixuan as zixuan_module


class _GlobalWatchlistDb:
    def __init__(self):
        self.groups = [
            SimpleNamespace(market="__global__", zx_group="我的关注"),
            SimpleNamespace(market="__global__", zx_group="跨市场观察"),
        ]
        self.stocks = [
            SimpleNamespace(
                market="a",
                zx_group="跨市场观察",
                stock_code="SH.600000",
                stock_name="浦发银行",
                stock_color="",
                stock_memo="A股",
                add_datetime=None,
            ),
            SimpleNamespace(
                market="hk",
                zx_group="跨市场观察",
                stock_code="00700",
                stock_name="腾讯控股",
                stock_color="#ff5722",
                stock_memo="港股",
                add_datetime=None,
            ),
        ]
        self.replacements = []

    def zx_get_global_groups(self):
        return list(self.groups)

    def zx_add_global_group(self, name):
        if any(group.zx_group == name for group in self.groups):
            return False
        self.groups.append(SimpleNamespace(market="__global__", zx_group=name))
        return True

    def zx_del_global_group(self, name):
        self.groups = [group for group in self.groups if group.zx_group != name]
        self.stocks = [stock for stock in self.stocks if stock.zx_group != name]
        return True

    def zx_get_global_group_stocks(self, name):
        return [stock for stock in self.stocks if stock.zx_group == name]

    def zx_replace_group_stocks(self, market, group, stocks):
        self.replacements.append((market, group, stocks))
        return True


def test_group_identity_is_global_and_member_market_is_preserved(
    monkeypatch,
):
    fake = _GlobalWatchlistDb()
    monkeypatch.setattr(zixuan_module, "db", fake)

    a_share = zixuan_module.ZiXuan("a")
    hong_kong = zixuan_module.ZiXuan("hk")

    assert a_share.zx_names == hong_kong.zx_names
    assert a_share.zx_names.count("我的关注") == 1
    assert "我的持仓" in a_share.zx_names
    assert a_share.zx_stocks("跨市场观察") == [
        {
            "market": "a",
            "code": "SH.600000",
            "name": "浦发银行",
            "color": "",
            "memo": "A股",
            "add_datetime": None,
        },
        {
            "market": "hk",
            "code": "00700",
            "name": "腾讯控股",
            "color": "#ff5722",
            "memo": "港股",
            "add_datetime": None,
        },
    ]


def test_market_scoped_replace_never_relabels_another_market_member(monkeypatch):
    fake = _GlobalWatchlistDb()
    monkeypatch.setattr(zixuan_module, "db", fake)
    watchlist = zixuan_module.ZiXuan("a")

    assert watchlist.replace_zx_stocks(
        "跨市场观察",
        [
            {"market": "hk", "code": "00700", "name": "腾讯控股"},
            {"market": "a", "code": "SH.600000", "name": "浦发银行"},
            {"code": "SZ.000001", "name": "平安银行"},
        ],
    )

    assert fake.replacements == [
        (
            "a",
            "跨市场观察",
            [
                {
                    "code": "SH.600000",
                    "name": "浦发银行",
                    "color": "",
                    "memo": "",
                },
                {
                    "code": "SZ.000001",
                    "name": "平安银行",
                    "color": "",
                    "memo": "",
                },
            ],
        )
    ]


def test_system_holding_group_is_global_and_cannot_be_deleted(monkeypatch):
    fake = _GlobalWatchlistDb()
    monkeypatch.setattr(zixuan_module, "db", fake)
    watchlist = zixuan_module.ZiXuan("a")

    assert watchlist.del_zx_group("我的持仓") is False
    assert "我的持仓" in {group.zx_group for group in fake.groups}
