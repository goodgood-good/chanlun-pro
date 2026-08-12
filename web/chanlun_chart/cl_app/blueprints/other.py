"""
杂项接口蓝图。

  - `/ticks`
"""

import json
import math
from dataclasses import replace

from flask import Blueprint, current_app, request
from flask_login import login_required

from chanlun.market import Market
from chanlun.exchange import get_exchange, market_now_trading
from chanlun.tools.log_util import LogUtil


other_bp = Blueprint("other", __name__)

# /ticks 单次请求的代码数量上限：保护后端不被超大列表打爆，
# 也覆盖正常前端使用场景（自选页面通常 < 200 个）。
_MAX_TICK_CODES = 500

_VALID_MARKETS = {m.value for m in Market}


def _us_tdx_fallback_ticks(codes):
    """Fetch US watchlist quotes from the existing TDX adapter.

    uSMART remains the configured US source.  This fallback is deliberately
    scoped to the lightweight ``/ticks`` watchlist endpoint so a transient
    uSMART TLS/read timeout cannot leave every US row blank.  Chart history,
    structure calculation and any decision evidence keep their configured
    source unchanged.
    """

    from chanlun.exchange.exchange_tdx_us import ExchangeTDXUS

    provider_to_project = {}
    for project_code in codes:
        value = str(project_code).strip()
        provider_code = value[:-3] if value.upper().endswith(".US") else value
        if provider_code:
            provider_to_project[provider_code.upper()] = value
    if not provider_to_project:
        return {}

    provider_ticks = ExchangeTDXUS().ticks(list(provider_to_project))
    ticks = {}
    for provider_code, tick in provider_ticks.items():
        project_code = provider_to_project.get(str(provider_code).upper())
        if project_code is not None and tick is not None:
            ticks[project_code] = replace(tick, code=project_code)
    return ticks


def _fetch_ticks_with_us_fallback(ex, market: str, codes):
    """Preserve primary quotes and fill missing US rows from TDX."""

    primary_error = None
    try:
        primary_ticks = ex.ticks(codes)
    except Exception as exc:
        primary_error = exc
        primary_ticks = {}

    if market != Market.US.value:
        if primary_error is not None:
            raise primary_error
        return primary_ticks

    missing = [code for code in codes if code not in primary_ticks]
    if not missing:
        return primary_ticks
    try:
        fallback_ticks = _us_tdx_fallback_ticks(missing)
    except Exception as fallback_error:
        LogUtil.warning(
            "/ticks US fallback failed "
            f"missing={len(missing)} error={type(fallback_error).__name__}"
        )
        if primary_error is not None:
            raise primary_error from fallback_error
        return primary_ticks

    if fallback_ticks:
        primary_ticks.update(fallback_ticks)
        LogUtil.warning(
            "/ticks US quotes used TDX fallback "
            f"coverage={len(fallback_ticks)}/{len(missing)} "
            f"primary_error={type(primary_error).__name__ if primary_error else 'none'}"
        )
    if not primary_ticks and primary_error is not None:
        raise primary_error
    return primary_ticks


def _error_response(code: str, message: str, status_code: int):
    return {
        "ok": False,
        "market_state": "unknown",
        "now_trading": None,
        "ticks": [],
        "error": {"code": code, "message": message},
    }, status_code


def _success_response(now_trading, ticks):
    if now_trading is None:
        normalized_now_trading = None
        market_state = "unknown"
    else:
        normalized_now_trading = bool(now_trading)
        market_state = "open" if normalized_now_trading else "closed"
    return {
        "ok": True,
        "market_state": market_state,
        "now_trading": normalized_now_trading,
        "ticks": ticks,
        "error": None,
    }


@other_bp.route("/ticks", methods=["POST"])
@login_required
def ticks():
    market = request.form.get("market", "")
    codes_raw = request.form.get("codes", "")

    if market not in _VALID_MARKETS:
        return _error_response("invalid_market", "Unsupported market.", 400)

    try:
        codes = json.loads(codes_raw)
    except (json.JSONDecodeError, TypeError):
        return _error_response("invalid_codes_json", "codes must be valid JSON.", 400)

    if not isinstance(codes, list):
        return _error_response("codes_not_list", "codes must be a JSON list.", 400)
    if len(codes) > _MAX_TICK_CODES:
        return _error_response("too_many_codes", "codes exceeds the maximum size.", 400)
    # 元素必须是字符串，避免下游交易所 SDK 收到非法类型崩溃。
    if not all(isinstance(c, str) for c in codes):
        return _error_response("code_must_be_string", "each code must be a string.", 400)

    try:
        ex = get_exchange(Market(market))
        stock_ticks = _fetch_ticks_with_us_fallback(ex, market, codes)
        try:
            now_trading = market_now_trading(ex, market)
        except Exception as exc:
        # 市场时段元数据仅供参考。保留成功获取的价格并暴露未知状态，
        # 不要丢弃整批结果。
            LogUtil.warning(
                f"/ticks market state unavailable market={market} err={exc}"
            )
            now_trading = None
        # rate 可为 None（盈透经 Redis 透传或币安 ccxt 缺少 percentage），原列表推导中
        # float(None) 抛 TypeError 被外层 except 吞→整批(含健康标的)清空且 now_trading=False
        # 停掉前端轮询。改逐标的隔离 + `or 0` 守零, 镜像 /tv/quotes(tv.py:637)。
        res_ticks = []
        for _c, _t in stock_ticks.items():
            if _t is None or _t.last is None:
                continue
            try:
                _price = float(_t.last)
                _rate = round(float(_t.rate or 0), 2)
                # NaN/Inf 不是合法 JSON（Flask 的 allow_nan=True 会输出裸 NaN 标记，
                # 打断前端严格 JSON.parse → 整批含健康标的全失败), 镜像 /tv/quotes(tv.py:654)降级跳过。
                if not math.isfinite(_price) or not math.isfinite(_rate):
                    continue
                res_ticks.append({"code": _c, "price": _price, "rate": _rate})
            except Exception:
                LogUtil.exception(
                    f"/ticks tick convert failed market={market} code={_c}"
                )
                continue
        readiness = current_app.extensions.get("readiness")
        market_closed = now_trading is not None and not bool(now_trading)
        if readiness is not None:
            if res_ticks or (codes and market_closed):
                readiness.record_ticks_success(market)
            elif codes:
                readiness.record_ticks_failure(
                    market,
                    "empty_result",
                    "Tick service returned no usable data.",
                )
        if codes and not res_ticks and not market_closed:
            return _error_response(
                "empty_result",
                "Tick service returned no usable data.",
                503,
            )
        return _success_response(now_trading, res_ticks)
    except Exception:
        # 完整堆栈仅写日志，避免直接暴露给前端调用方。
        readiness = current_app.extensions.get("readiness")
        if readiness is not None:
            readiness.record_ticks_failure(
                market,
                "service_unavailable",
                "Tick service is temporarily unavailable.",
            )
        LogUtil.exception(f"/ticks failed market={market} codes_len={len(codes)}")
        return _error_response("service_unavailable", "Tick service is temporarily unavailable.", 503)
