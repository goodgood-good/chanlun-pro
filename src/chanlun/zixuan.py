from typing import List, Dict

from chanlun import fun
from chanlun.market import Market
from chanlun.persistence.db import db
from sqlalchemy.exc import IntegrityError

from chanlun.exchange import get_exchange

_log = fun.get_logger()

DEFAULT_ZX_GROUP = "我的关注"
MANUAL_HOLDING_ZX_GROUP = "我的持仓"
SYSTEM_ZX_GROUPS = (DEFAULT_ZX_GROUP, MANUAL_HOLDING_ZX_GROUP)


class ZiXuan(object):
    """
    自选池功能
    """

    def __init__(self, market_type):
        self.market_type = market_type
        self.zixuan_list = self.get_zx_groups()

        self.zx_names = [_zx["name"] for _zx in self.zixuan_list]

    def get_zx_groups(self):
        zx_groups = db.zx_get_global_groups()
        names = {group.zx_group for group in zx_groups}
        for required_group in SYSTEM_ZX_GROUPS:
            if required_group in names:
                continue
            try:
                db.zx_add_global_group(required_group)
            except IntegrityError:
                # 并发构造时，另一请求可能已经创建了同一个全局组。
                pass
        zx_groups = db.zx_get_global_groups()
        return [{"name": _g.zx_group} for _g in zx_groups]

    def add_zx_group(self, zx_group_name):
        if zx_group_name in SYSTEM_ZX_GROUPS:
            return False
        if zx_group_name in [_z["name"] for _z in self.zixuan_list]:
            return False
        try:
            created = db.zx_add_global_group(zx_group_name)
        except IntegrityError:
            # 并发下另一线程抢先建同名全局组，幂等视为已存在。
            return False
        if not created:
            return False
        self.zixuan_list = self.get_zx_groups()
        self.zx_names = [_zx["name"] for _zx in self.zixuan_list]
        return True

    def del_zx_group(self, zx_group_name):
        if zx_group_name in SYSTEM_ZX_GROUPS:
            return False
        if zx_group_name not in self.zx_names:
            return False
        db.zx_del_global_group(zx_group_name)
        self.zixuan_list = self.get_zx_groups()
        self.zx_names = [_zx["name"] for _zx in self.zixuan_list]
        return True

    def query_all_zs_stocks(self):
        """
        查询自选分组下所有的代码信息
        """
        return [
            {"zx_name": zx_name, "stocks": self.zx_stocks(zx_name)}
            for zx_name in self.zx_names
        ]

    def zx_stocks(self, zx_group) -> List[Dict[str, object]]:
        """
        根据自选名称，获取其中的 代码列表
        """
        if zx_group not in self.zx_names:
            return []
        stocks = db.zx_get_global_group_stocks(zx_group)
        result: List[Dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for stock in stocks:
            identity = (stock.market, stock.stock_code)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(
                {
                    "market": stock.market,
                    "code": stock.stock_code,
                    "name": stock.stock_name,
                    "color": stock.stock_color,
                    "memo": stock.stock_memo,
                    "add_datetime": stock.add_datetime,
                }
            )
        return result

    def add_stock(
        self, zx_group: str, code: str, name: str, location="bottom", color="", memo=""
    ):
        """
        添加自选

        #ff5722  红色
        #ffb800  橙色
        #16baaa  绿色
        #1e9fff  蓝色
        #a233c6  紫色

        """
        if zx_group not in self.zx_names:
            return False
        # 名称为空时自动通过交易所接口获取；拉取失败则以 code 兜底
        if name is None or name == "" or name == "undefined":
            try:
                ex = get_exchange(Market(self.market_type))
                stock_info = ex.stock_info(code)
                name = stock_info["name"]
            except Exception as exc:
                # 拉取失败时不能继续写空名（否则 UI 上是空白条目，且后续无人知道为什么），
                # 用 code 兜底，并记录 warning 让运维定位真实原因。
                _log.warning(
                    f"[ZiXuan.add_stock] fetch stock_info failed, "
                    f"market={self.market_type} code={code}, fallback name=code, err={exc}"
                )
                name = code
        db.zx_add_group_stock(
            self.market_type, zx_group, code, name, memo, color, location
        )
        return True

    def del_stock(self, zx_group, code):
        """
        删除自选中的代码
        """
        db.zx_del_group_stock(self.market_type, zx_group, code)
        return True

    def color_stock(self, zx_group, code, color):
        """
        给指定的代码加上颜色
        """
        db.zx_update_stock_color(self.market_type, zx_group, code, color)
        return True

    def sort_top_stock(self, zx_group, code):
        """
        将股票排在最上面
        """
        db.zx_stock_sort_top(self.market_type, zx_group, code)
        return True

    def sort_bottom_stock(self, zx_group, code):
        """
        将股票排在最下面
        """
        db.zx_stock_sort_bottom(self.market_type, zx_group, code)
        return True

    def replace_zx_stocks(self, zx_group: str, stocks: List[Dict[str, str]]) -> bool:
        """解析标的名称后，以单事务完整替换目标自选组。"""
        if zx_group not in self.zx_names:
            return False

        normalized = []
        ex = None
        for stock in stocks:
            stock_market = stock.get("market")
            if stock_market is not None and stock_market != self.market_type:
        # ``replace_zx_stocks`` 仍是供特定市场选股或导入任务使用的有范围写入；
        # 数据库操作会保留同一全局分组中其他市场的成员。
                continue
            code = stock["code"]
            name = stock.get("name")
            if not name:
                try:
                    if ex is None:
                        ex = get_exchange(Market(self.market_type))
                    stock_info = ex.stock_info(code)
                    name = stock_info["name"] if stock_info else code
                except Exception as exc:
                    _log.warning(
                        f"[ZiXuan.replace_zx_stocks] fetch stock_info failed, "
                        f"market={self.market_type} code={code}, fallback name=code, err={exc}"
                    )
                    name = code
            normalized.append(
                {
                    "code": code,
                    "name": name,
                    "color": stock.get("color", ""),
                    "memo": stock.get("memo", ""),
                }
            )

        return db.zx_replace_group_stocks(self.market_type, zx_group, normalized)

    def query_code_zx_names(self, code):
        """
        查询代码所在的自选分组
        """
        exists_group = db.zx_query_group_by_code(self.market_type, code)
        res_zx_group = [
            {
                "zx_name": _g["name"],
                "code": code,
                "exists": 1 if _g["name"] in exists_group else 0,
            }
            for _g in self.zixuan_list
        ]
        return res_zx_group
