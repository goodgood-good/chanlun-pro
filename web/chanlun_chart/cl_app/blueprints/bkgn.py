"""
板块/概念相关接口蓝图。

  - `/a/bkgn_list`
  - `/a/bkgn_codes`
"""

from flask import Blueprint, request
from flask_login import login_required

from chanlun.market import Market
from chanlun.exchange import get_exchange, resolve_bounded_stock_info
from chanlun.exchange.stocks_bkgn import StocksBKGN
from ..services.trading_screening_scope import admit_explicit_validation_codes


bkgn_bp = Blueprint("bkgn", __name__)


@bkgn_bp.route("/a/bkgn_list", methods=["GET"])
@login_required
def a_bkgn_list():
    """
    获取沪深a股市场的板块列表
    """
    stock_bkgn = StocksBKGN()
    bkgn_infos = stock_bkgn.file_bkgns()
    all_hy_names = bkgn_infos["hys"]
    all_gn_names = bkgn_infos["gns"]

    res_bkgn_list = []
    for _hy in all_hy_names:
        res_bkgn_list.append(
            {
                "type": "hy",
                "bkgn_name": f"行业:{_hy}",
                "bkgn_code": _hy,
            }
        )
    for _gn in all_gn_names:
        res_bkgn_list.append(
            {
                "type": "gn",
                "bkgn_name": f"概念:{_gn}",
                "bkgn_code": _gn,
            }
        )
    return {
        "code": 0,
        "msg": "",
        "data": res_bkgn_list,
        "count": len(res_bkgn_list),
    }


@bkgn_bp.route("/a/bkgn_codes", methods=["POST"])
@login_required
def a_bkgn_codes():
    bkgn_type = request.form["bkgn_type"]
    bkgn_code = request.form["bkgn_code"]
    stock_bkgn = StocksBKGN()

    if bkgn_type == "hy":
        codes = stock_bkgn.ths_to_tdx_codes(stock_bkgn.get_codes_by_hy(bkgn_code))
    elif bkgn_type == "gn":
        codes = stock_bkgn.ths_to_tdx_codes(stock_bkgn.get_codes_by_gn(bkgn_code))
    else:
        codes = []

    if not codes:
        return {"code": 0, "msg": "", "data": {}, "count": 0}
    try:
        admitted_codes = admit_explicit_validation_codes(codes, max_symbols=20)
    except ValueError:
        return {
            "code": 1,
            "msg": "板块成员超过普通接口 20 只上限，请提供显式小范围代码",
            "data": {},
            "count": 0,
        }, 400

    ex = get_exchange(Market.A)
    stocks = {}
    for _code in admitted_codes:
        _stock = resolve_bounded_stock_info(
            ex,
            _code,
            allow_code_fallback=True,
        )
        if _stock is not None:
            stocks[_code] = _stock

    return {"code": 0, "msg": "", "data": stocks, "count": len(stocks)}
