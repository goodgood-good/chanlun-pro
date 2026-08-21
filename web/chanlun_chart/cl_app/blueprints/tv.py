"""
TradingView 相关接口蓝图。

提供 /tv/config、/tv/symbols、/tv/search、/tv/history 等标准 UDF 接口，
以及图表/模板/画线存取和自定义 Marks 支持。
"""
import pytz
import json
import math
import datetime
import time
from flask import Blueprint, current_app, request
from flask_login import login_required

from chanlun import fun
from chanlun.market import Market
from chanlun.cl_utils import (
    query_cl_chart_config,
)
from chanlun.persistence.db import db
from chanlun.exchange import get_exchange
from chanlun.tools.log_util import LogUtil

from ..services.constants import (
    frequency_maps,
    resolution_maps,
    market_frequencys,
    market_session,
    market_timezone,
    market_types,
)
from ..services.last_chart_state import record_user_request
from ..services.realtime_quotes import isolated_a_share_quote_batch

tv_bp = Blueprint("tv", __name__)

# 图表缓存、symbols 预加载、跨周期 MACD 等基础设施均已迁至 services 子包，
from ..services.chart_cache import (  # noqa: E402
    _build_cache_key,
    _get_chart_cache_entry,
    _set_chart_cache_entry,
    cache_lock,
    evaluate_cache_for_tv_history,
)


# ---------------------------------------------------------------------------
# tv_history 响应列对齐（按 bar index 的数值列必须与 t 等长）
# ---------------------------------------------------------------------------
# 前端按 index 取 c/o/h/l/v[i] 与 macd_*[i]/higher_macd_*[i]（上界 = t.length），任一列短于 t →
# 越界处取到 undefined → 静默 NaN（K 线缺口 / MACD 面板空洞，无异常无日志，最难排查）。正常计算路径
# 恒等长，但不完整磁盘缓存经 slice / 合并后可能错位。基础形态对象数组
# （fxs/bis/xds）长度本就 != bar 数，不在此列。
_TV_VALUE_COLUMNS = (
    "c", "o", "h", "l", "v",
    "macd_dif", "macd_dea", "macd_hist", "macd_area",
    "higher_macd_dif", "higher_macd_dea", "higher_macd_hist",
)


def _align_value_columns_to_t(cl_chart_data, symbol="", resolution=""):
    """把所有按 bar index 的数值列原地对齐到 len(t)：过长截断、过短右 pad None。

    仅当列非空且长度 != len(t) 时才动（空 / 缺列保持"无数据"语义，与既有 OHLCV 守卫一致）。
    """
    _t_col = cl_chart_data.get("t", []) or []
    _n_bars = len(_t_col)
    for _col_k in _TV_VALUE_COLUMNS:
        _col = cl_chart_data.get(_col_k) or []
        if _col and len(_col) != _n_bars:
            LogUtil.warning(
                f"[tv_history] 列 {_col_k} 长 {len(_col)} != t {_n_bars}, 已对齐 {symbol} {resolution}"
            )
            cl_chart_data[_col_k] = list(_col[:_n_bars]) + [None] * max(0, _n_bars - len(_col))

from ..services.user_activity import _mark_user_request  # noqa: E402
# stock_list 服务：symbols 预加载、缓存、读取
from ..services.stock_list import (  # noqa: E402
    get_cached_processed_stock,
    get_cached_processed_stocks,
)
# chart_compute 服务：锁注册表 + chart 序列化 + 主计算路径
from ..services.chart_compute import (  # noqa: E402
    chart_calc_locks,
    fetch_klines_and_compute_cl_data,
    market_now_trading,
    slice_chart_data_to_window,
    strict_structure_history_fields,
    _decide_full_snapshot,
    trim_future_bars,
)
from ..services.chart_revalidate import submit_revalidation  # noqa: E402


def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _normalize_unix_ts(value, default=0):
    ts = _safe_int(value, default)
    if ts > 10**12:
        ts = ts // 1000
    return ts


def _normalize_resolution(resolution: str):
    if resolution is None:
        return None
    return {"D": "1D", "W": "1W", "M": "1M"}.get(resolution, resolution)


def _validated_review_chart_lock():
    """返回服务端校验通过的人工复核因果图表锁。"""

    values = {
        "candidate_id": str(request.args.get("review_candidate_id") or ""),
        "source_sha256": str(request.args.get("review_source_sha256") or ""),
        "review_as_of": str(request.args.get("review_as_of") or ""),
    }
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise ValueError("partial human-review chart lock")
    service = current_app.extensions.get("decision_support_human_review")
    validator = getattr(service, "validate_chart_lock", None)
    if not callable(validator):
        raise ValueError("人工复核图表锁服务不可用")
    return validator(
        candidate_id=values["candidate_id"],
        source_sha256=values["source_sha256"],
        review_as_of=int(values["review_as_of"]),
    )


def _parse_tv_symbol(symbol: str):
    if not symbol:
        return None, None
    symbol = symbol.strip()
    if ":" in symbol:
        market, code = symbol.split(":", 1)
        market = market.lower().strip()
        code = code.strip()
        if market in market_types and code != "":
            return market, code
    upper_symbol = symbol.upper()
    if upper_symbol.endswith(".US"):
        return "us", upper_symbol
    return None, None


def _drawing_storage_name(chart_id: str, layout_id: str, symbol: str, resolution: str):
    return f"drawings_{layout_id}_{chart_id}_{symbol}_{resolution}"


_USER_DRAWING_STATE_SCHEMA = "chanlun-user-drawings"


def _empty_user_drawing_state():
    """Return the only drawing-state shape accepted by the current UI.

    Automatic Chanlun entities are reconstructed from chart data and must never
    be restored from TradingView's line-tool persistence. Schema-less state is
    rejected rather than inferred.
    """

    return {
        "schema": _USER_DRAWING_STATE_SCHEMA,
        "sources": {},
        "groups": {},
    }


def _normalize_user_drawing_state(value):
    """Normalize an explicit current-schema manual-drawing state."""

    if not isinstance(value, dict):
        return None
    if value.get("schema") != _USER_DRAWING_STATE_SCHEMA:
        return None
    sources = value.get("sources")
    groups = value.get("groups")
    if not isinstance(sources, dict) or not isinstance(groups, dict):
        return None
    return {
        "schema": _USER_DRAWING_STATE_SCHEMA,
        "sources": {
            str(source_id): source_state
            for source_id, source_state in sources.items()
            if isinstance(source_state, dict)
        },
    # 自动生成的实体绝不写入持久化 TradingView 分组。
        "groups": {},
    }


@tv_bp.route("/tv/config")
@login_required
def tv_config():
    supportedResolutions = list(frequency_maps.values())
    return {
        "supports_search": True,
        "supported_resolutions": supportedResolutions,
        "supports_time": False,
        "exchanges": [
            {"value": "a", "name": "沪深", "desc": "沪深A股"},
            {"value": "hk", "name": "港股", "desc": "港股"},
            {"value": "fx", "name": "外汇", "desc": "外汇"},
            {"value": "us", "name": "美股", "desc": "美股"},
            {"value": "futures", "name": "国内期货", "desc": "国内期货"},
            {"value": "ny_futures", "name": "纽约期货", "desc": "纽约期货"},
            {
                "value": "currency",
                "name": "数字货币(Futures)",
                "desc": "数字货币（合约）",
            },
            {
                "value": "currency_spot",
                "name": "数字货币(Spot)",
                "desc": "数字货币（现货）",
            },
        ],
    }


@tv_bp.route("/tv/symbols")
@login_required
def tv_symbols():
    raw_symbol: str = request.args.get("symbol", "")
    market, code = _parse_tv_symbol(raw_symbol)
    if market is None or code is None:
        return {"s": "error", "errmsg": f"invalid symbol: {raw_symbol}"}

    try:
        _validated_review_chart_lock()
    except (TypeError, ValueError, RuntimeError) as exc:
        LogUtil.warning(f"[tv_symbols] rejected review lock: {exc}")
        return {"s": "error", "errmsg": "invalid causal chart lock"}
    # 先读已恢复的 last-known-good symbol 缓存。冷启动时 QMT 的全市场刷新会长时间
    # 持有 xtdata native lock；若这里先调 stock_info，前端 Requester 会在 15 秒后超时并把
    # 一次临时阻塞永久记成 unknown_symbol，直到用户手动“重新加载数据”。缓存命中时不再
    # 调用 stock_info / xtdata native lock，保证 TradingView 首次 resolveSymbol 能立即完成。
    stocks = get_cached_processed_stock(market, code)
    ex = None
    if stocks is None:
        try:
            ex = get_exchange(Market(market))
        except Exception as e:
            LogUtil.error(f"[tv_symbols] get_exchange failed symbol={raw_symbol} err={e}")
            return {"s": "error", "errmsg": "invalid market"}

        try:
            stocks = ex.stock_info(code)
        except Exception as e:
            # 数据源故障(如 QMT/xtquant 不可用)时优雅降级为 error,不抛到 flask 变 500。
            LogUtil.error(f"[tv_symbols] stock_info failed symbol={raw_symbol} err={e}")
            return {"s": "error", "errmsg": f"unknown symbol: {raw_symbol}"}
        if stocks is None:
            return {"s": "error", "errmsg": f"unknown symbol: {raw_symbol}"}

    if "code" not in stocks:
        stocks["code"] = code
    if "name" not in stocks:
        stocks["name"] = code

    sector = ""
    industry = ""
    if market == "a" and ex is not None:
        try:
            gnbk = ex.stock_owner_plate(code)
            sector = " / ".join([_g["name"] for _g in gnbk["GN"]])
            industry = " / ".join([_h["name"] for _h in gnbk["HY"]])
        except Exception:
            pass

    # precision 缺失或非法时使用 K 线精度的同一规则；A 股磁盘 LKG 为控制体积不保存
    # precision，因此 ETF/基金需按代码恢复到 1000，普通股票恢复到 100。
    precision = stocks.get("precision")
    if precision is None:
        # 外汇(tdx_fx)stock_info 也不带 precision；与 K 线归一精度对齐，避免截断。
        if market in ("a", "fx"):
            from chanlun.exchange.kline_precision import resolve_decimals

            _dec = resolve_decimals(market, code)
            precision = 10 ** _dec if _dec is not None else 100
        else:
            precision = 100
    else:
        try:
            precision = int(precision)
            if precision <= 0:
                precision = 100
        except (TypeError, ValueError):
            precision = 100

    supported_resolutions = [
        v for k, v in frequency_maps.items() if k in market_frequencys.get(market, [])
    ]
    info = {
        "name": stocks["code"],
        "ticker": f"{market}:{stocks['code']}",
        "full_name": f"{market}:{stocks['code']}",
        "description": stocks["name"],
        "exchange": market,
        "listed_exchange": market,
        "type": market_types.get(market, "stock"),
        "session": market_session.get(market, "24x7"),
        "timezone": market_timezone.get(market, "Asia/Shanghai"),
        "minmov": 1,
        "pricescale": precision,

        "visible_plots_set": "ohlcv",
        "supported_resolutions": supported_resolutions,
        "intraday_multipliers": [
            resolution for resolution in supported_resolutions if resolution.isdigit()
        ],
        "has_intraday": True,
        "has_seconds": True if market in ["futures", "ny_futures"] else False,
        "has_daily": True,
        "has_weekly_and_monthly": True,
        "sector": sector,
        "industry": industry,
    }
    return info


# /tv/quotes 单次请求标的数上限(自选组通常 < 100, 防超大列表打爆数据源)。
_MAX_QUOTE_SYMBOLS = 500


@tv_bp.route("/tv/quotes")
@login_required
def tv_quotes():
    """TradingView UDF 行情接口 —— 自选组(watchlist)实时报价来源。

    前端 datafeed 的 ``QuotesPulseProvider`` 按 Fast/General 定时器调 ``getQuotes``
    打 ``/tv/quotes?symbols=a:SH.513100,a:SZ.000001``, 据此周期性刷新自选列表的
    现价/涨跌幅。**缺此端点时前端每次请求 404 → 自选组行情不自动更新**(本次修复)。

    复用 ``ex.ticks()``(与 ``/ticks`` 同一取数口径), 按 market 分组批量取, 返回
    UDF 标准格式 ``{s:"ok", d:[{s:"ok", n:symbol, v:{lp,ch,chp,...}}]}``。Tick 不带
    昨收, 由现价 + 涨跌幅% 反推昨收与绝对涨跌额。单个 market 取数失败仅该组标记
    error, 不拖垮整批(自选常含多市场)。
    """
    symbols_raw = request.args.get("symbols", "")
    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
    if not symbols:
        return {"s": "ok", "d": []}
    symbols = symbols[:_MAX_QUOTE_SYMBOLS]

    # 按 market 分组: market -> {code: 原始 symbol}(返回的 n 字段须与请求字面一致,
    # 否则 TradingView 匹配不上不更新)。
    by_market: dict = {}
    data = []
    for sym in symbols:
        market, code = _parse_tv_symbol(sym)
        if market is None or code is None:
            data.append({"s": "error", "n": sym, "v": {}})
            continue
        by_market.setdefault(market, {})[code] = sym

    for market, code_map in by_market.items():
        try:
            isolated_batch = (
                isolated_a_share_quote_batch(current_app, list(code_map))
                if market == Market.A.value
                else None
            )
            if isolated_batch is not None:
                stock_ticks = isolated_batch.ticks()
            else:
                ex = get_exchange(Market(market))
                stock_ticks = ex.ticks(list(code_map.keys()))
        except Exception:
            LogUtil.exception(
                f"[tv_quotes] ticks failed market={market} n={len(code_map)}"
            )
            for sym in code_map.values():
                data.append({"s": "error", "n": sym, "v": {}})
            continue
        if not isinstance(stock_ticks, dict):
            LogUtil.warning(
                f"[tv_quotes] ticks returned invalid payload market={market} "
                f"type={type(stock_ticks).__name__}"
            )
            stock_ticks = {}
        for code, sym in code_map.items():
            t = stock_ticks.get(code)
            if t is None or t.last is None:
                data.append({"s": "error", "n": sym, "v": {}})
                continue
            try:
                last = float(t.last)
                rate = float(t.rate or 0)
                # rate 为涨跌幅百分比(Tick 文档口径)→ 反推昨收: prev = last/(1+rate/100)。
                prev_close = last / (1 + rate / 100) if rate != -100 else last
                open_p = float(t.open) if t.open is not None else last
                high_p = float(t.high) if t.high is not None else last
                low_p = float(t.low) if t.low is not None else last
                volume = float(t.volume) if t.volume is not None else 0.0
            except Exception:
                # 单个标的字段转换异常仅标记该标的 error, 不拖垮同批其它标的。
                LogUtil.exception(
                    f"[tv_quotes] tick convert failed market={market} code={code}"
                )
                data.append({"s": "error", "n": sym, "v": {}})
                continue
            # NaN/Infinity 不是合法 JSON: Flask(allow_nan=True) 会原样输出裸 NaN token,
            # 打断前端 JSON.parse → 整批(含健康标的)报价更新失败。命中即降级该标的,
            # 决不让非有限值进入最终 JSON(NaN 与任何数比较均为 False, rate!=-100 拦不住)。
            if not all(
                math.isfinite(x)
                for x in (last, rate, prev_close, open_p, high_p, low_p, volume)
            ):
                data.append({"s": "error", "n": sym, "v": {}})
                continue
            data.append({
                "s": "ok",
                "n": sym,
                "v": {
                    "lp": last,
                    "ch": round(last - prev_close, 4),
                    "chp": round(rate, 2),
                    "open_price": open_p,
                    "high_price": high_p,
                    "low_price": low_p,
                    "prev_close_price": round(prev_close, 4),
                    "volume": volume,
                },
            })
    return {"s": "ok", "d": data}


@tv_bp.route("/tv/search")
@login_required
def tv_search():
    # 关键修复：搜索结果必须严格按"当前页面市场"过滤，避免 A 股搜出美股之类的串市场问题。
    # 触发原因：TradingView 的 Symbol Search 组件默认会传 exchange="" / "All" / 上次选中的
    # 交易所，并不一定等于当前 chart 的 market；若后端不校验直接命中错误缓存或 KeyError 退化，
    # 会让前端 datafeed 回退到内置 symbol 列表（含历史浏览过的其它市场标的）。
    query = (request.args.get("query") or "").strip()
    type_ = request.args.get("type")
    exchange = (request.args.get("exchange") or "").strip().lower()
    try:
        limit = int(request.args.get("limit", "10"))
    except (TypeError, ValueError):
        limit = 10
    if limit <= 0:
        limit = 10

    # exchange 必须是已知市场之一，否则直接拒绝；不要静默回退到任何"看似合理"的市场。
    if exchange not in market_types or exchange not in market_frequencys:
        LogUtil.warning(
            f"[tv_search] reject invalid exchange={exchange!r} query={query!r}"
        )
        return {"error": f"invalid exchange: {exchange!r}"}, 400

    # 空 query 直接返回空列表，避免对几万条 symbol 全量扫描后被 limit 截断成"看起来随机"的结果。
    if not query:
        return []

    # 用 allow_sync_fallback=True: 启动后 60s 预加载空窗期或某个市场首次访问时, 同步加载一次,
    # 避免直接 500。最差情况是返回 [], 搜索框显示"无结果"——优于"接口异常"的体感。
    try:
        processed_stocks = get_cached_processed_stocks(exchange, allow_sync_fallback=True)
    except Exception as e:
        LogUtil.error(f"[tv_search] get stocks failed exchange={exchange}: {e}")
        # 兜底也失败时仍降级为空列表而不是 500, 避免前端 datafeed 抛异常显示"加载错误"。
        processed_stocks = []

    if not processed_stocks:
        # 没有可搜的 symbol 直接返回空, 后续逻辑还有 market_session/market_timezone 取值,
        # 提前返回也能省一次循环。
        return []

    query_lower = query.lower()
    is_currency = exchange in ["currency", "currency_spot"]

    # 优先级：完全相等 > code/拼音前缀 > 任意子串包含。
    # 这样搜 "600" 不会被一堆名字含 600 的票淹没；搜 "中国" 也不会被代码含相同字符的票打乱顺序。
    exact_hits = []
    prefix_hits = []
    contains_hits = []
    for stock in processed_stocks:
        code_l = stock['code_lower']
        name_l = stock['name_lower']
        pinyin_l = stock['pinyin_initials']

        if is_currency:
            if query_lower == code_l:
                exact_hits.append(stock)
            elif code_l.startswith(query_lower):
                prefix_hits.append(stock)
            elif query_lower in code_l:
                contains_hits.append(stock)
        else:
            if query_lower == code_l or query_lower == name_l:
                exact_hits.append(stock)
            elif (code_l.startswith(query_lower)
                  or pinyin_l.startswith(query_lower)
                  or name_l.startswith(query_lower)):
                prefix_hits.append(stock)
            elif (query_lower in code_l
                  or query_lower in name_l
                  or query_lower in pinyin_l):
                contains_hits.append(stock)

        # 早停：精确+前缀已经够用就不再扫剩下的，节省 CPU。
        if len(exact_hits) + len(prefix_hits) >= limit:
            break

    res_stocks = (exact_hits + prefix_hits + contains_hits)[:limit]

    # 用 .get 防御 market_frequencys 中 exchange 因懒加载失败缺键的情况（前面已校验在表内，
    # 但懒加载 build 失败时值会是 []，这里再兜一层就不会抛）。
    supported_resolutions = [
        v for k, v in frequency_maps.items() if k in market_frequencys.get(exchange, [])
    ]
    session_value = market_session.get(exchange, "24x7")
    timezone_value = market_timezone.get(exchange, "Asia/Shanghai")

    infos = []
    for stock in res_stocks:
        infos.append(
            {
                "symbol": stock["code"],
                "name": stock["code"],
                "full_name": f"{exchange}:{stock['code']}",
                "description": stock["name"],
                "exchange": exchange,
                "ticker": f"{exchange}:{stock['code']}",
                "type": type_,
                "session": session_value,
                "timezone": timezone_value,
                "supported_resolutions": supported_resolutions,
            }
        )
    return infos

@tv_bp.route("/tv/history")
@login_required
def tv_history():
    _req_start_ts = time.time()
    try:
        args = request.args.to_dict()
        symbol = request.args.get("symbol", "")
        resolution = _normalize_resolution(request.args.get("resolution"))
        firstDataRequest = request.args.get("firstDataRequest", "false")
        _from = _normalize_unix_ts(request.args.get("from", "0"))
        _to = _normalize_unix_ts(request.args.get("to", "0"))
        try:
            _review_lock = _validated_review_chart_lock()
        except (TypeError, ValueError, RuntimeError) as exc:
            LogUtil.warning(f"[tv_history] rejected human-review lock: {exc}")
            return {"s": "no_data"}
        _review_as_of = (
            None if _review_lock is None else int(_review_lock["review_as_of"])
        )
        if _review_as_of is not None:
            _to = _review_as_of if _to <= 0 else min(_to, _review_as_of)
            if _from > _review_as_of:
                return {"s": "no_data"}
        # H1(阶段E): 前端断档 gap-reset 主动带 force_refresh=1 → 绕过缓存强制重算,补齐断档。
        # 绕过而非删缓存:走既有 MISS→重算路径,重算失败旧 entry 仍在(符合 C1"绝不丢好缓存")。
        force_refresh = request.args.get("force_refresh") == "1"
        tz_sh = pytz.timezone("Asia/Shanghai")

        def _fmt_ts(ts: int) -> str:
            """把 unix 时间戳格式化为上海时区可读时间;非正值(缺省 0)原样返回。"""
            if ts <= 0:
                return str(ts)
            return datetime.datetime.fromtimestamp(ts, tz_sh).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        LogUtil.info(
            f"[tv_history] >>> {symbol} {resolution} first={firstDataRequest} "
            f"from={_fmt_ts(_from)} to={_fmt_ts(_to)}"
        )

        if not symbol or not resolution:
            return {"s": "no_data"}
        if _from < 0 and _to < 0:
            return {"s": "no_data"}

        market, code = _parse_tv_symbol(symbol)
        if market is None or code is None:
            LogUtil.warning(f"[tv_history] invalid symbol: {symbol}")
            return {"s": "no_data"}
        if _review_lock is not None and (
            market != "a" or code != _review_lock.get("symbol")
        ):
            LogUtil.warning("[tv_history] review lock symbol mismatch")
            return {"s": "no_data"}
        frequency = resolution_maps.get(resolution)
        if frequency is None:
            LogUtil.warning(f"[tv_history] Unsupported resolution: {resolution}")
            return {"s": "no_data"}
        # 后端闸门:frequency 必须在该 market 实际支持的周期内(= 前端 supported_resolutions 的来源)。
        # 前端已按此过滤, 但手构请求(curl/改 URL)可绕过——传入 market 不支持的周期(如季线 q 对 cq
        # 美股/港股、qmt A股), 会落到各 exchange 不一致的处理(cq 返回空 / qmt·binance frequency_map
        # KeyError 抛 500 / tdx 系碰巧 frequency_map 有 q 而拉季线)。统一在此干净拒绝, 后端不依赖前端
        # 闸门。market_frequencys[market] 为空(exchange 初始化失败)时跳过本检查, 避免误拦正常请求。
        _supported_freqs = market_frequencys.cached_snapshot((market,)).get(
            market, []
        )
        if _supported_freqs and frequency not in _supported_freqs:
            LogUtil.warning(
                f"[tv_history] market={market} 不支持周期 {frequency}(resolution={resolution}), 拒绝"
            )
            return {"s": "no_data"}

        # 标记用户活跃度，供批量预热（symbols.py）让位 / 优先插队使用。
        # 关键：仅 firstDataRequest=true（用户主动切标的/切周期）才标记活跃；
        # firstDataRequest=false 是 TradingView 后台 polling（每 ~3 秒 1 次），
        # 如果也算"用户活跃"，会把批量预热永久卡死。
        if firstDataRequest == "true":
            _mark_user_request(market, code)
            # 记录最后访问状态，供下次启动预热 RAM chart_data_cache；失败吞异常不影响主流程。
            try:
                record_user_request(market, code, frequency)
            except Exception:
                pass

        log_args = dict(args)
        for key in ("from", "to"):
            if key in log_args:
                ts = _normalize_unix_ts(log_args.get(key))
                if ts > 0:
                    try:
                        log_args[key] = datetime.datetime.fromtimestamp(ts, tz_sh).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    except Exception:
                        pass
        LogUtil.debug(f"tv_history request args: {log_args}")

        req_tag = f"{symbol}|{resolution}|{firstDataRequest}|{_from}->{_to}"
        cl_config = query_cl_chart_config(market, code)
        if not isinstance(cl_config, dict):
            cl_config = {}
        # 使用稳定 hash 构造 cache_key（不受 PYTHONHASHSEED 影响，进程重启后仍一致）
        cache_key = _build_cache_key(market, code, frequency, cl_config)
        if _review_lock is not None:
            # 严禁与实时快照共享缓存：先按复核时点截断 K 线，再计算结构；不能
            # 在包含未来 K 线的结构结果上做事后裁剪。
            cache_key += (
                f"_review_{_review_lock['candidate_id'][7:]}_{_review_as_of}"
            )

        cl_chart_data = None
        is_cache_hit = False
        cache_miss_reason = "cache_empty"
        is_range_request = (
            firstDataRequest == "false"
            and _from > 0
            and _to > 0
            and _to >= _from
        )
        if firstDataRequest == "false" and not is_range_request:
            # false 本意是 polling/向左滚动,但 from/to 缺失或非法(畸形请求或 resolution 切换瞬间)
            # 会退化进非 range 分支、可能回吐整条全量(审查 M-3,触发面窄)。记 debug 便于线上定位,
            # 不改行为(非 range 分支自带 stale 兜底)。
            LogUtil.debug(f"[tv_history] false 但非 range from={_from} to={_to} {code} {frequency}")

        # 注意：必须先 get 出 RLock 对象再 with，确保整个临界区内引用持续存在
        # （_SafeLockRegistry 用 WeakValueDictionary 存储锁，无强引用会被 GC）
        # 方向2: 交易时段决定 serve-stale 的过期阈值(盘中短/收盘长)。在锁外算
        # (带 30s TTL 缓存), 不占用 cache_lock 临界区。
        _market_trading = (
            False if _review_lock is not None else market_now_trading(market)
        )
        _needs_refresh = False
        _calc_lock = chart_calc_locks.get(cache_key)
        with _calc_lock:
        # 内存未命中时可能同步读取 pickle；将该输入输出放在进程级缓存锁之外，
        # 每个键的计算锁仍会串行化写入。
            cache_entry = _get_chart_cache_entry(cache_key)
            if _review_lock is not None and cache_entry is not None:
                # 历史复核输入不可变，禁止 live stale-revalidate 用当前行情覆盖它。
                cache_entry = {**cache_entry, "validated_at": time.time()}
            with cache_lock:
                is_cache_hit, cl_chart_data, miss_reason, _needs_refresh = (
                    evaluate_cache_for_tv_history(
                        cache_entry, _from, _to, is_range_request,
                        market_is_trading=_market_trading,
                        force_refresh=force_refresh,
                    )
                )
                if not is_cache_hit:
                    cache_miss_reason = miss_reason

            # D4-F1/F2: 用与 cl_chart_data 同源的 is_full(cache-hit 取本次 entry), 避免 1050 行锁外
            # 重取 entry 产生 TOCTOU(窄 local + 并发全量写 entry → gate 误判 → 前端整体替换丢窗外形态)。
            _src_is_full = bool(cache_entry and cache_entry.get("is_full_snapshot", False))

            if not is_cache_hit:
                # 早返: 请求范围完全早于(或刚好接到)缓存最早时间 -> 必无数据.
                # TradingView UDF 翻页时下一次请求的 _to 正好等于上次的 cache_min_time,
                # 用 <= 才能覆盖这种边界. 切片逻辑 bisect_left 是左闭右开, _to == min_time
                # 时切片 [0:0] 仍为空, 语义一致.
                # 不早返会触发行情拉取和严格结构计算。
                if (
                    is_range_request
                    and _to > 0
                    and cache_entry is not None
                    and cache_entry.get("min_time") is not None
                    and _to <= cache_entry["min_time"]
                ):
                    # 此处不 mark validated:"请求窗口早于缓存最早时间"只证明这个窄窗口无数据,
                    # 不证明整条 entry 末端新鲜。进程重启后命中过期磁盘 entry 时若误标 fresh,
                    # 随后的 tail_gap polling 会命中缺停机期 K 线的旧数据、绕过 stale 兜底(审查 H-2)。
                    return {"s": "no_data"}

                LogUtil.debug(f"[tv_history] Cache miss ({cache_miss_reason}) req={req_tag}")
                kline_args = {}
                # cache_empty(冷缓存,缓存里完全没有该 cache_key)即便是窄范围轮询
                # 请求,也必须按默认回看窗口全量拉取。否则空缓存会被窄窗口请求"种小"
                # 成只有几根 K 线的 entry,后续 prepend 又把它标成 is_full_snapshot=True,
                # 导致 firstDataRequest=true 命中这个"假全量"快照只返回几根 K 线。
                if is_range_request and cache_miss_reason != "cache_empty":
                    kline_args["start_date"] = datetime.datetime.fromtimestamp(
                        _from, tz=tz_sh
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    kline_args["end_date"] = datetime.datetime.fromtimestamp(
                        _to, tz=tz_sh
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    LogUtil.debug(
                        f"[tv_history] incremental request {code} range: {kline_args['start_date']} -> {kline_args['end_date']}"
                    )
                else:
                    end_at = (
                        datetime.datetime.fromtimestamp(_review_as_of, tz_sh)
                        if _review_as_of is not None
                        else datetime.datetime.now(tz_sh)
                    )
                    kline_args["end_date"] = end_at.strftime("%Y-%m-%d %H:%M:%S")

                _fetch_result = fetch_klines_and_compute_cl_data(
                    market, code, frequency, cl_config,
                    kline_args=kline_args,
                    is_range_request=is_range_request,
                    cache_miss_reason=cache_miss_reason,
                    cache_key=cache_key,
                    to_ts=_to,
                )
                if _fetch_result is None:
                    return {"s": "no_data"}
                cl_chart_data = _fetch_result["cl_chart_data"]
                _src_is_full = _fetch_result["is_full_snapshot"]
                _cache_already_written = _fetch_result["cache_already_written"]
                # D4-F1/F2: MISS 全量性与 entry 写入 is_full_snapshot 同口径(非range/cache_empty/prepend cd None→全量)。

                if not _cache_already_written:
                    # 全量重算结果直接替换 existing_entry，避免保留失效形态。
                    # 这里走到的都是 MISS 全量重算(cache_empty/cache_partial_snapshot/
                    # cache_stale_snapshot 等), cl_chart_data 本身就是基于完整回看窗口的
                    # 全量权威结果。existing_entry 可能是几分钟到几天前的陈旧快照——
                    # too_stale(cache_stale_snapshot)分支存在的目的就是防止把陈旧未完成
                    # 笔/线段/中枢泄漏给用户, 若仍用"起点身份并集"合并, 陈旧快照里起点
                    # 已被新行情证伪的形态会被原样保留、和新数据一起返回, 安全网形同虚设。
                    with cache_lock:
                        _set_chart_cache_entry(
                            cache_key,
                            cl_chart_data,
                            # cache_empty 已按全量回看拉取(见上方 kline_args 分支),
                            # 与非范围请求同样是完整快照,标 is_full_snapshot=True。
                            # ⚠ 不再继承 existing_entry 的 is_full_snapshot:range-miss 是窄窗口结果,
                            # 继承会把"窄范围 merge 进旧全量"误标成完整快照,令 firstDataRequest 命中
                            # 只有几根 K 线的假全量(审查 H-1,目前仅靠 tail_gap 改道 prepend 侥幸不触发)。
                            is_full_snapshot=_src_is_full,
                        )

        # 方向1 (stale-while-revalidate): firstDataRequest 命中"过期全量快照"已即时
        # 返回旧快照(秒显), 这里派去重的后台重验证拉全新数据写回缓存, 经现有
        # SSE 推送 / TV polling 自愈到前端。submit 非阻塞, 不影响本次响应延迟。
        if is_cache_hit and _needs_refresh and _review_lock is None:
            submit_revalidation(market, code, frequency, cl_config, cache_key)

        if cl_chart_data is None:
            return {"s": "no_data"}

        bar_times = cl_chart_data.get("t", [])
        # D4-F1: 纯轮询/SSE 降级下 /tv/history 轮询响应原按窄窗口 slice 形态且不带 full_snapshot,
        # 前端窗口外"只增不删" -> 起点早于窄窗口的被撤销形态幽灵残留(SSE 正常 ~8s 自愈, 纯轮询/
        # low2high 不自愈)。仅"最近窗口权威 + 源全量快照"时带全量形态 + full_snapshot=True, 前端
        # 已有整体替换分支清幽灵、bars/MACD 仍走增量不缩图; 向左滚动/窄窗口不置(防丢窗外合法形态)。
        # D4-F1/F2: _src_is_full 已在 _calc_lock 内与 cl_chart_data 同源捕获(消 TOCTOU), 此处不重取 entry。
        _strict_source_data = cl_chart_data
        _emit_full_snapshot = _decide_full_snapshot(
            firstDataRequest,
            _to,
            bar_times,
            _src_is_full,
            frequency=frequency,
        )
        _full_shape_snapshot = None
        if _emit_full_snapshot:
            _shape_keys = ("fxs", "bis", "xds")
            _full_shape_snapshot = {_k: cl_chart_data.get(_k) for _k in _shape_keys}
        if not is_cache_hit:
            _fxs_cnt = len(cl_chart_data.get("fxs", []))
            _bis_cnt = len(cl_chart_data.get("bis", []))
            _xds_cnt = len(cl_chart_data.get("xds", []))
            LogUtil.debug(
                f"[tv_history] Calc Finish & Cached req={req_tag}, bars={len(bar_times)}, fxs={_fxs_cnt}, bis={_bis_cnt}, xds={_xds_cnt}"
            )
        else:
            LogUtil.debug(f"[tv_history] Cache Hit req={req_tag}, bars={len(bar_times)}")

        if firstDataRequest == "false" and len(bar_times) > 0:
            try:
                cl_chart_data = slice_chart_data_to_window(
                    cl_chart_data,
                    _from,
                    _to,
                    frequency=frequency,
                )
            except Exception as e:
                LogUtil.error(f"[tv_history] Slice data failed: {e}")

        # 切片后无数据,返回 no_data 阻止 TradingView 继续向前请求
        if len(cl_chart_data.get("t", [])) == 0:
            return {"s": "no_data"}

        _resp_times = cl_chart_data.get("t", []) or []
        cl_chart_data = trim_future_bars(
            cl_chart_data,
            _to,
            frequency=frequency,
        )
        if _full_shape_snapshot is not None:
            # D4-F1: slice 已把形态切窄, 换回全量供前端整体替换清幽灵(bars 保持窗口化不缩图)。
            for _k, _v in _full_shape_snapshot.items():
                if _v is not None:
                    cl_chart_data[_k] = _v
        _resp_t = cl_chart_data.get("t", []) or []
        if len(_resp_t) < len(_resp_times):
            LogUtil.warning(
                f"[tv_history] Trimmed {len(_resp_times) - len(_resp_t)} future bar(s) beyond to={_to}"
            )
        if not _resp_t:
            return {"s": "no_data"}

        # 严格快照按原始收盘时刻做身份校验；日/周/月仅在裁剪与图表坐标层
        # 使用周期锚点。把协议字段放到最终窗口确定之后，避免响应 t 已裁短而
        # strict_structure.source_closed_at 仍指向被裁掉的末根。
        _strict_history_fields = strict_structure_history_fields(
            _strict_source_data,
            authoritative=(
                firstDataRequest == "true" or _emit_full_snapshot
            ),
            expected_source_closed_at=_resp_t[-1],
        )

        LogUtil.debug(
            f"[DataVerify][Backend] symbol={symbol} resolution={resolution} "
            f"update={firstDataRequest != 'true'} bars={len(_resp_t)} "
            f"fxs={len(cl_chart_data.get('fxs', []))} "
            f"bis={len(cl_chart_data.get('bis', []))} "
            f"xds={len(cl_chart_data.get('xds', []))} "
            f"strict={_strict_history_fields.get('strict_structure_mode')}"
        )

        _elapsed_ms = (time.time() - _req_start_ts) * 1000
        LogUtil.info(
            f"[tv_history] {symbol} {resolution} bars={len(_resp_t)} "
            f"first={firstDataRequest} elapsed={_elapsed_ms:.0f}ms"
        )

        # 按 bar index 的数值列（OHLCV + macd_* + higher_macd_*）必须与 t 等长，否则前端越界取
        # undefined → 静默 NaN（无异常无日志，最难排查，审查 F-1/MED-3）。统一对齐见 _align_value_columns_to_t。
        _align_value_columns_to_t(cl_chart_data, symbol, resolution)

        return {
            "s": "ok",
            "t": cl_chart_data.get("t", []),
            "c": cl_chart_data.get("c", []),
            "o": cl_chart_data.get("o", []),
            "h": cl_chart_data.get("h", []),
            "l": cl_chart_data.get("l", []),
            "v": cl_chart_data.get("v", []),
            "macd_dif": cl_chart_data.get("macd_dif", []),
            "macd_dea": cl_chart_data.get("macd_dea", []),
            "macd_hist": cl_chart_data.get("macd_hist", []),
            "macd_area": cl_chart_data.get("macd_area", []),
            "higher_macd_dif": cl_chart_data.get("higher_macd_dif", []),
            "higher_macd_dea": cl_chart_data.get("higher_macd_dea", []),
            "higher_macd_hist": cl_chart_data.get("higher_macd_hist", []),
            "fxs": cl_chart_data.get("fxs", []),
            "bis": cl_chart_data.get("bis", []),
            "xds": cl_chart_data.get("xds", []),
            "update": False if firstDataRequest == "true" else True,
            "full_snapshot": _emit_full_snapshot,
            **_strict_history_fields,
        }
    except Exception as e:
        req_qs = request.query_string.decode("utf-8", errors="ignore")
        LogUtil.error(f"[tv_history] unhandled error query={req_qs} err={e}", exc_info=True)
        return {
            "s": "error",
            "errmsg": "History service is temporarily unavailable.",
        }, 503


@tv_bp.route("/tv/time")
@login_required
def tv_time():
    return fun.datetime_to_int(datetime.datetime.now())


@tv_bp.route("/tv/<api_revision>/charts", methods=["GET", "POST", "DELETE"])
@login_required
def tv_charts(api_revision):
    del api_revision  # TradingView storage API 路径契约要求保留该段。
    client_id = str(request.args.get("client"))
    user_id = str(request.args.get("user"))

    if request.method == "GET":
        chart_id = request.args.get("chart")
        if chart_id is None:
            chart_list = db.tv_chart_list("chart", client_id, user_id)
            return {
                "status": "ok",
                "data": [
                    {
                        "timestamp": c.timestamp,
                        "symbol": c.symbol,
                        "resolution": c.resolution,
                        "id": c.id,
                        "name": c.name,
                    }
                    for c in chart_list
                ],
            }
        else:
            chart = db.tv_chart_get("chart", chart_id, client_id, user_id)
            if chart is None:
                # chart_id 不存在（已删除 / 脏 id）→ 返回 error，前端
                # getChartContent 对 status!='ok' 取 null，优雅降级，不 500。
                return {"status": "error"}
            return {
                "status": "ok",
                "data": {
                    "content": chart.content,
                    "timestamp": chart.timestamp,
                    "name": chart.name,
                    "id": chart.id,
                },
            }
    elif request.method == "DELETE":
        chart_id = request.args.get("chart")
        db.tv_chart_del("chart", chart_id, client_id, user_id)
        return {
            "status": "ok",
        }
    else:
        name = request.form["name"]
        content = request.form["content"]
        symbol = request.form["symbol"]
        resolution = request.form["resolution"]
        chart_id = request.args.get("chart")

        if chart_id is None:
            id = db.tv_chart_save(
                "chart", client_id, user_id, name, content, symbol, resolution
            )
            return {
                "status": "ok",
                "id": id,
            }
        else:
            db.tv_chart_update(
                "chart",
                chart_id,
                client_id,
                user_id,
                name,
                content,
                symbol,
                resolution,
            )
            return {"status": "ok"}


@tv_bp.route("/tv/<api_revision>/study_templates", methods=["GET", "POST", "DELETE"])
@login_required
def tv_study_templates(api_revision):
    del api_revision  # TradingView storage API 路径契约要求保留该段。
    client_id = str(request.args.get("client"))
    user_id = str(request.args.get("user"))

    if request.method == "GET":
        template = request.args.get("template")
        if template is None:
            template_list = db.tv_chart_list("template", client_id, user_id)
            return {
                "status": "ok",
                "data": [{"name": t.name} for t in template_list],
            }
        else:
            template = db.tv_chart_get_by_name(
                "template", template, client_id, user_id
            )
            if template is None:
                # template 不存在 → 返回 error，避免 None.name 抛 AttributeError。
                return {"status": "error"}
            return {
                "status": "ok",
                "data": {"name": template.name, "content": template.content},
            }
    elif request.method == "DELETE":
        name = request.args.get("template")
        db.tv_chart_del_by_name("template", name, client_id, user_id)
        return {
            "status": "ok",
        }
    else:
        name = request.form["name"]
        content = request.form["content"]
        db.tv_chart_save("template", client_id, user_id, name, content, "", "")
        return {"status": "ok"}


@tv_bp.route("/tv/<api_revision>/drawings", methods=["GET", "POST"])
@login_required
def tv_drawings(api_revision):
    del api_revision  # TradingView storage API 路径契约要求保留该段。
    client_id = str(request.args.get("client"))
    user_id = str(request.args.get("user"))
    chart_id = request.args.get("chart", "default")
    layout_id = request.args.get("layout", "default")
    symbol = request.args.get("symbol", "")
    resolution = request.args.get("resolution", "")

    drawing_name = _drawing_storage_name(chart_id, layout_id, symbol, resolution)
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if request.is_json and not isinstance(payload, dict):
            return {
                "status": "error",
                "message": "JSON body must be an object.",
            }, 400
        payload = payload or {}
        content = payload.get("state")
        if not isinstance(content, dict):
            return {
                "status": "error",
                "message": "state must be a JSON object.",
            }, 400
        normalized = _normalize_user_drawing_state(content)
        if normalized is None:
            return {
                "status": "error",
                "message": "unsupported drawing state schema",
            }, 400
        db.tv_chart_save(
            "drawing",
            client_id,
            user_id,
            drawing_name,
            json.dumps(normalized, ensure_ascii=False, sort_keys=True),
            symbol,
            resolution,
        )
        return {"status": "ok"}

    if request.method == "GET":
        drawing = db.tv_chart_get_by_name("drawing", drawing_name, client_id, user_id)
        if drawing:
            try:
                data = _normalize_user_drawing_state(json.loads(drawing.content))
            except Exception:
                data = None
            return {
                "status": "ok",
                "data": data or _empty_user_drawing_state(),
            }
        return {
            "status": "ok",
            "data": _empty_user_drawing_state(),
        }
