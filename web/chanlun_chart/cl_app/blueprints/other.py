"""
杂项接口蓝图。

  - `/ticks`
"""

import json
import math

from flask import Blueprint, current_app, request
from flask_login import login_required

from chanlun.market import Market
from chanlun.exchange import get_exchange, market_now_trading
from chanlun.tools.log_util import LogUtil
from ..services.external_tick_backoff import ExternalMarketTickBackoff
from ..services.realtime_quotes import isolated_a_share_quote_batch


other_bp = Blueprint("other", __name__)

# /ticks 单次请求的代码数量上限：保护后端不被超大列表打爆，
# 也覆盖正常前端使用场景（自选页面通常 < 200 个）。
_MAX_TICK_CODES = 500

_VALID_MARKETS = {m.value for m in Market}
_EXTERNAL_TICK_BACKOFF_EXTENSION = "external_market_tick_backoff"
_EXTERNAL_TICK_SHARED_MAX_AGE_SECONDS = 2.0
_EXTERNAL_TICK_COALESCE_WAIT_SECONDS = 1.0


def _error_response(
    code: str,
    message: str,
    status_code: int,
    *,
    retry_after_seconds: int | None = None,
):
    error: dict[str, object] = {"code": code, "message": message}
    if type(retry_after_seconds) is int and retry_after_seconds > 0:
        error["retry_after_seconds"] = retry_after_seconds
    return {
        "ok": False,
        "market_state": "unknown",
        "now_trading": None,
        "ticks": [],
        "error": error,
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


def _shared_success_response(shared, requested_codes):
    payload = dict(shared.payload)
    requested = set(requested_codes)
    ticks = payload.get("ticks")
    if isinstance(ticks, list):
        filtered_ticks = [
            tick
            for tick in ticks
            if isinstance(tick, dict) and tick.get("code") in requested
        ]
        # A larger cached batch may have omitted one requested symbol while
        # still succeeding for its other symbols. Do not turn that omission
        # into a false successful response for a narrower request.
        if requested and not filtered_ticks and payload.get("market_state") != "closed":
            return None
        payload["ticks"] = filtered_ticks
    else:
        return None
    payload["quote_state"] = "shared"
    payload["quote_age_seconds"] = round(float(shared.age_seconds), 3)
    return payload


def _deferred_response(retry_after_seconds: int):
    return {
        "ok": True,
        "quote_state": "deferred",
        "retry_after_seconds": max(1, int(retry_after_seconds)),
        "market_state": "unknown",
        "now_trading": None,
        "ticks": [],
        "error": None,
    }


def _external_tick_backoff() -> ExternalMarketTickBackoff:
    existing = current_app.extensions.get(_EXTERNAL_TICK_BACKOFF_EXTENSION)
    if existing is None:
        existing = current_app.extensions.setdefault(
            _EXTERNAL_TICK_BACKOFF_EXTENSION,
            ExternalMarketTickBackoff(),
        )
    if not isinstance(existing, ExternalMarketTickBackoff):
        raise RuntimeError("external market tick backoff extension is invalid")
    return existing


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

    external_backoff = None
    external_probe_id = None
    if market != Market.A.value and codes:
        external_backoff = _external_tick_backoff()
        shared = external_backoff.recent_success(
            market,
            codes,
            max_age_seconds=_EXTERNAL_TICK_SHARED_MAX_AGE_SECONDS,
        )
        if shared is not None:
            shared_response = _shared_success_response(shared, codes)
            if shared_response is not None:
                return shared_response
        permit = external_backoff.acquire(market)
        if not permit.allowed:
            if permit.reason_code == "PROVIDER_PROBE_IN_FLIGHT":
                shared = external_backoff.wait_for_success(
                    market,
                    codes,
                    not_before=permit.probe_started_at,
                    timeout_seconds=_EXTERNAL_TICK_COALESCE_WAIT_SECONDS,
                    max_age_seconds=_EXTERNAL_TICK_SHARED_MAX_AGE_SECONDS,
                )
                if shared is not None:
                    shared_response = _shared_success_response(shared, codes)
                    if shared_response is not None:
                        return shared_response
                # The first request may have completed with another code set or
                # failed while this request waited. Re-check once so this call
                # either fetches its missing codes or inherits the real backoff.
                permit = external_backoff.acquire(market)
                if (
                    not permit.allowed
                    and permit.reason_code == "PROVIDER_PROBE_IN_FLIGHT"
                ):
                    return _deferred_response(permit.retry_after_seconds)
            if not permit.allowed:
                readiness = current_app.extensions.get("readiness")
                if readiness is not None:
                    readiness.record_ticks_failure(
                        market,
                        "service_unavailable",
                        "Tick provider retry is temporarily deferred.",
                    )
                return _error_response(
                    "service_unavailable",
                    "Tick service is temporarily unavailable.",
                    503,
                    retry_after_seconds=permit.retry_after_seconds,
                )
        external_probe_id = permit.probe_id

    try:
        isolated_batch = (
            isolated_a_share_quote_batch(current_app, codes)
            if market == Market.A.value
            else None
        )
        if isolated_batch is not None:
            stock_ticks = isolated_batch.ticks()
            now_trading = isolated_batch.market_open
        else:
            ex = get_exchange(Market(market))
            # 每个市场只读取配置好的唯一适配器。美股由盈立提供报价、长桥提供所配置的
            # 历史数据，不再在接口内部混入通达信价格。
            stock_ticks = ex.ticks(codes)
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
            if external_backoff is not None:
                # The provider answered, so failure backoff can close, but an
                # unusable open-market batch must not enter the shared cache.
                external_backoff.record_success(
                    market,
                    probe_id=external_probe_id,
                )
            return _error_response(
                "empty_result",
                "Tick service returned no usable data.",
                503,
            )
        response_payload = _success_response(now_trading, res_ticks)
        if external_backoff is not None:
            external_backoff.record_success(
                market,
                probe_id=external_probe_id,
                requested_codes=codes,
                response_payload=response_payload,
            )
        return response_payload
    except Exception:
        # 完整堆栈仅写日志，避免直接暴露给前端调用方。
        readiness = current_app.extensions.get("readiness")
        if readiness is not None:
            readiness.record_ticks_failure(
                market,
                "service_unavailable",
                "Tick service is temporarily unavailable.",
            )
        retry_after_seconds = None
        if external_backoff is not None:
            retry_after_seconds = external_backoff.record_failure(
                market,
                probe_id=external_probe_id,
            ).retry_after_seconds
        LogUtil.exception(f"/ticks failed market={market} codes_len={len(codes)}")
        return _error_response(
            "service_unavailable",
            "Tick service is temporarily unavailable.",
            503,
            retry_after_seconds=retry_after_seconds,
        )
