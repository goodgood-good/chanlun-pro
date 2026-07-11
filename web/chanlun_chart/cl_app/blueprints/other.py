"""
杂项接口蓝图。

  - `/ticks`
"""

import json
import math

from flask import Blueprint, request
from flask_login import login_required

from chanlun.market import Market
from chanlun.exchange import get_exchange
from chanlun.tools.log_util import LogUtil


other_bp = Blueprint("other", __name__)

# /ticks 单次请求的代码数量上限：保护后端不被超大列表打爆，
# 也覆盖正常前端使用场景（自选页面通常 < 200 个）。
_MAX_TICK_CODES = 500

_VALID_MARKETS = {m.value for m in Market}


@other_bp.route("/ticks", methods=["POST"])
@login_required
def ticks():
    market = request.form.get("market", "")
    codes_raw = request.form.get("codes", "")

    if market not in _VALID_MARKETS:
        return {"now_trading": False, "ticks": [], "errmsg": "invalid_market"}

    try:
        codes = json.loads(codes_raw)
    except (json.JSONDecodeError, TypeError):
        return {"now_trading": False, "ticks": [], "errmsg": "invalid_codes_json"}

    if not isinstance(codes, list):
        return {"now_trading": False, "ticks": [], "errmsg": "codes_not_list"}
    if len(codes) > _MAX_TICK_CODES:
        return {"now_trading": False, "ticks": [], "errmsg": "too_many_codes"}
    # 元素必须是字符串，避免下游交易所 SDK 收到非法类型崩溃。
    if not all(isinstance(c, str) for c in codes):
        return {"now_trading": False, "ticks": [], "errmsg": "code_must_be_string"}

    try:
        ex = get_exchange(Market(market))
        stock_ticks = ex.ticks(codes)
        now_trading = ex.now_trading()
        # R4-C6: rate 可为 None(ib 透传 redis / binance ccxt percentage 缺省), 原列表推导内
        # float(None) 抛 TypeError 被外层 except 吞→整批(含健康标的)清空且 now_trading=False
        # 停掉前端轮询。改逐标的隔离 + `or 0` 守零, 镜像 /tv/quotes(tv.py:637)。
        res_ticks = []
        for _c, _t in stock_ticks.items():
            if _t is None or _t.last is None:
                continue
            try:
                _price = _t.last
                _rate = round(float(_t.rate or 0), 2)
                # R14-C1: NaN/Inf 不是合法 JSON(Flask allow_nan=True 输出裸 NaN token
                # 打断前端严格 JSON.parse → 整批含健康标的全失败), 镜像 /tv/quotes(tv.py:654)降级跳过。
                if not math.isfinite(_price) or not math.isfinite(_rate):
                    continue
                res_ticks.append({"code": _c, "price": _price, "rate": _rate})
            except Exception:
                LogUtil.exception(
                    f"/ticks tick convert failed market={market} code={_c}"
                )
                continue
        return {"now_trading": now_trading, "ticks": res_ticks}
    except Exception:
        # 完整堆栈仅写日志，避免直接暴露给前端调用方。
        LogUtil.exception(f"/ticks failed market={market} codes_len={len(codes)}")
        return {"now_trading": False, "ticks": [], "errmsg": "internal_error"}
