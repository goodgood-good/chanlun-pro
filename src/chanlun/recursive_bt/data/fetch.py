"""chanlun.recursive_bt.data.fetch — 用 QMT 拉前复权 K线 + 预计算缠论信号,缓存本地。

CL 重计算(慢)只在此做一次,缓存 {dates,open,close,small_by_bar,big_dir_at,limit_pct} 到 out_dir/{code}.pkl。
可断点续传(已存在跳过)。涨跌停按板块:科创688/创业300/301=20%,主板=10%。
- 默认(近1年)5m小级别+30m大级别 → bt_data:   python -m chanlun.recursive_bt.data.fetch 沪深300
- 日线级(2022~2024,熊市验证)1d小+1w大 → bt_data_daily: python -m chanlun.recursive_bt.data.fetch daily
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pickle
import sys
import time

import pandas as pd

from chanlun.recursive_bt.engine.engine import (
    collect_branch_signals, CL_CFG, MTFStrategy,
)
from chanlun.core.cl import CL

OUT = "D:/chanlun_pro/bt_data"
OUT_DAILY = "D:/chanlun_pro/bt_data_daily"
OUT_MTF3 = "D:/chanlun_pro/bt_data_mtf3"
OUT_MTF3_ALL_A = "D:/chanlun_pro/bt_data_mtf3_all_a"
OUT_ALL_A = "D:/chanlun_pro/bt_data_all_a"
SIGNAL_SCHEMA = {"nest_operable": True, "nest_key": 3}
ALL_A_ALIASES = {"全A股", "全A", "all_a", "alla", "all"}
ALL_A_SECTORS = ("沪深A股", "沪深京A股", "上证A股", "深证A股", "北证A股")
BOARD_ALIASES = {
    "all": None,
    "shsz": {"main", "gem", "star"},
    "non_bj": {"main", "gem", "star"},
    "non-bj": {"main", "gem", "star"},
}
BOARD_ORDER = ("main", "gem", "star", "bj")
SAMPLE_MODES = {"stratified", "sorted"}


def universe(sector: str, limit=None):
    from xtquant import xtdata
    xtdata.download_sector_data()
    codes = xtdata.get_stock_list_in_sector(sector) or []
    out = [f"{c.split('.')[1]}.{c.split('.')[0]}" for c in codes]   # '600000.SH'→'SH.600000'
    out.sort()
    return out[:limit] if limit else out


def universe_all_a(limit=None):
    from xtquant import xtdata
    xtdata.download_sector_data()
    seen = set()
    out = []
    for sector in ALL_A_SECTORS:
        for raw in xtdata.get_stock_list_in_sector(sector) or []:
            if "." not in raw:
                continue
            num, mkt = raw.split(".")
            if mkt not in ("SH", "SZ", "BJ"):
                continue
            code = f"{mkt}.{num}"
            if code in seen:
                continue
            seen.add(code)
            out.append(code)
    out.sort()
    return out[:limit] if limit else out


def limit_pct(code: str) -> float:
    board = ashare_board(code)
    if board == "bj":
        return 0.30
    if board in {"gem", "star"}:
        return 0.20
    return 0.10


def ashare_board(code: str) -> str:
    code = (code or "").strip().upper()
    if code.startswith("BJ."):
        return "bj"
    num = code.split(".")[1]
    if num.startswith("8") or num.startswith("920"):
        return "bj"
    if num.startswith("688"):
        return "star"
    if num.startswith(("300", "301")):
        return "gem"
    return "main"


def parse_board_filter(value: str | None) -> set[str] | None:
    raw = (value or "all").strip().lower()
    if raw in BOARD_ALIASES:
        alias = BOARD_ALIASES[raw]
        return None if alias is None else set(alias)
    boards: set[str] = set()
    for item in raw.replace(";", ",").split(","):
        board = item.strip().lower()
        if not board:
            continue
        if board in BOARD_ALIASES and BOARD_ALIASES[board] is not None:
            boards.update(BOARD_ALIASES[board] or set())
        elif board in BOARD_ORDER:
            boards.add(board)
    return boards or None


def filter_codes_by_board(codes, board_filter: str | None):
    boards = parse_board_filter(board_filter)
    if not boards:
        return list(codes)
    return [code for code in codes if ashare_board(code) in boards]


def sample_codes_by_board(codes, limit: int | None, sample_mode: str = "stratified"):
    items = sorted(codes)
    if not limit or limit <= 0 or len(items) <= limit:
        return items
    mode = (sample_mode or "stratified").strip().lower()
    if mode == "sorted":
        return items[:limit]

    grouped: dict[str, list[str]] = {board: [] for board in BOARD_ORDER}
    for code in items:
        grouped.setdefault(ashare_board(code), []).append(code)
    active = [board for board in BOARD_ORDER if grouped.get(board)]
    selected: list[str] = []
    while active and len(selected) < limit:
        next_active: list[str] = []
        for board in active:
            bucket = grouped.get(board) or []
            if bucket and len(selected) < limit:
                selected.append(bucket.pop(0))
            if bucket:
                next_active.append(board)
        active = next_active
    return selected


def _parse_limit_and_board(args: list[str]) -> tuple[int | None, str | None]:
    limit: int | None = None
    board_filter: str | None = None
    for arg in args:
        value = str(arg).strip()
        if not value:
            continue
        if value.isdigit() and limit is None:
            limit = int(value)
        elif board_filter is None:
            board_filter = value
    return limit, board_filter


def _parse_limit_board_sample(args: list[str]) -> tuple[int | None, str | None, str]:
    limit: int | None = None
    board_filter: str | None = None
    sample_mode = "stratified"
    for arg in args:
        value = str(arg).strip()
        if not value:
            continue
        low = value.lower()
        if value.isdigit() and limit is None:
            limit = int(value)
        elif low in SAMPLE_MODES:
            sample_mode = low
        elif board_filter is None:
            board_filter = value
    return limit, board_filter, sample_mode


def _slice(df, start, end):
    if df is None or len(df) == 0:
        return df
    if start:
        df = df[df["date"] >= pd.Timestamp(start, tz="Asia/Shanghai")]
    if end:
        df = df[df["date"] <= pd.Timestamp(end, tz="Asia/Shanghai") + pd.Timedelta("1D")]
    return df.reset_index(drop=True)


def _sig(code, ex, tf, start, end, min_bars=30, annotate_nest=False):
    df = _slice(ex.klines(code, tf, start_date=start), start, end)
    # F-HIGH-2: 丢弃未收盘在制 bar,与 live(MonitorSymbolState._process_level)口径统一。
    # 盘中跑 fetch 构建 pkl 时,末根在制 bar 会让 small_by_bar/big_dir_at 基于会变的 bar
    # 计算、随后被 selector 当"已收盘事实"读,构成构建期前视(同 round-3 F-HIGH-1)。
    # pre-market 跑则末根已收盘,本调用 no-op(now 已过收盘),无害。local import 避循环依赖。
    from chanlun.recursive_bt.sim.paper import drop_unclosed_last_bar

    df = drop_unclosed_last_bar(df, tf)
    if df is None or len(df) < min_bars:
        return df, []
    cd = CL(code, tf, dict(CL_CFG))
    cd.process_klines(df)
    return df, collect_branch_signals(
        cd,
        use_xd=False,
        annotate_nest=annotate_nest,
    )


def _signals_by_main_bar(signals, dates, delay):
    out = {}
    ordered = sorted(signals, key=lambda sig: sig.date)
    si = 0
    for i, date in enumerate(dates):
        while si < len(ordered) and ordered[si].date + delay <= date:
            out.setdefault(i, []).append(ordered[si])
            si += 1
    return out


def _freq_delay(tf: str):
    tf = str(tf or "").strip().lower()
    if tf.endswith("m") and tf[:-1].isdigit():
        return pd.Timedelta(minutes=int(tf[:-1]))
    if tf in {"d", "day", "1d"}:
        return pd.Timedelta(days=1)
    if tf in {"w", "week", "1w"}:
        return pd.Timedelta(days=7)
    return pd.Timedelta(tf)


def _daily_bsp_and_d3(code, ex, dates, win_days: int = 10, bs_class: int = 3, start=None, end=None):
    """F-HIGH-3(audit/_round6_select_data.md):在 build 内算日线买卖点 + 日线N买共振窗口。

    selector 的 `_daily_resonance` 读 pkl['d3_ok'];旧 build() 不产该字段,而它**只**由
    回测侧 portfolio.attach_daily_bsp_window 写入 → live 缓存恒无 d3_ok → 日线3买共振优先级
    在实盘是死特性(排序退化为只按买点类别),与回测口径系统性不一致。这里把
    patch_daily_bsp(日线 CL→get_branch_bspoints)+ attach_daily_bsp_window(确认bar收盘
    次日起 win_days 自然日窗口)的等价逻辑并入 build,使 live 缓存带 d3_ok。
    口径与 portfolio.attach_daily_bsp_window 一致:win_days=10、bs_class=3、anchor +1D 次日生效
    (无 lookahead)。⚠这会改变 live 选股排序——日线3买共振票会被顶到前面(与回测对齐)。

    返回 (daily_bsp, d3_ok):daily_bsp=[(date, bs_type)];d3_ok=np.bool 数组(长度=len(dates))。
    日线取数失败/不足时返回 ([], 全 False) 以保证字段恒存在(语义=无日线共振,非缺字段)。
    """
    import numpy as np

    from chanlun.recursive_bt.sim.paper import drop_unclosed_last_bar

    n = len(dates)
    d3_ok = np.zeros(n, bool)
    daily_bsp: list[tuple] = []
    try:
        df = drop_unclosed_last_bar(ex.klines(code, "d", start_date=start, end_date=end), "d")
        if df is not None and len(df) >= 100:
            cd = CL(code, "d", dict(CL_CFG))
            cd.process_klines(df)
            daily_bsp = [(s.date, s.bs_type) for s in collect_branch_signals(cd, use_xd=False)]
    except Exception:
        return [], d3_ok

    key = f"{bs_class}buy" if bs_class else None     # None=日线任意类买点(宽口径)
    ev = [d for d, bt in daily_bsp if (bt == key if key else str(bt).endswith("buy"))]
    if ev:
        ei = 0
        active_until = None
        for i, t in enumerate(dates):
            while ei < len(ev) and ev[ei] + pd.Timedelta("1D") <= t:
                active_until = ev[ei] + pd.Timedelta(days=1 + win_days)
                ei += 1
            d3_ok[i] = active_until is not None and t <= active_until
    return daily_bsp, d3_ok


def build(code, ex, small_tf="5m", big_tf="30m", start=None, end=None,
          big_delay=None, min_small=200, mid_tf=None, mid_delay=None) -> dict | None:
    dfs, small = _sig(code, ex, small_tf, start, end, min_small, annotate_nest=True)
    if dfs is None or len(dfs) < min_small:
        return None
    _, big = _sig(code, ex, big_tf, start, end)
    dates = list(dfs["date"])
    strat = MTFStrategy(small, big, dates, "5m+30m", gate="not_down", big_delay=big_delay)
    daily_bsp, d3_ok = _daily_bsp_and_d3(code, ex, dates, start=start, end=end)
    out = {
        "code": code, "dates": dates,
        "open": dfs["open"].to_numpy(), "close": dfs["close"].to_numpy(),
        "small_by_bar": strat.small_by_bar, "big_dir_at": strat.big_dir_at,
        "limit_pct": limit_pct(code), "n_small": len(small), "n_big": len(big),
        "signal_schema": dict(SIGNAL_SCHEMA),
        # F-HIGH-3:日线3买共振窗口,使 live selector._daily_resonance 真正生效(口径同回测)。
        "daily_bsp": daily_bsp, "d3_ok": d3_ok,
    }
    if mid_tf:                       # 三级联立:中级别(如5m)方向门控(1m入场+5m中+30m大)
        _, mid = _sig(code, ex, mid_tf, start, end)
        sm = MTFStrategy(small, mid, dates, "5m+30m", gate="not_down", big_delay=mid_delay)
        out["mid_dir_at"] = sm.big_dir_at
        out["mid_by_bar"] = _signals_by_main_bar(
            mid,
            dates,
            mid_delay if mid_delay is not None else _freq_delay(mid_tf),
        )
        out["n_mid"] = len(mid)
    return out


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _atomic_pickle_dump(obj, path: str) -> None:
    """原子写 pickle:先写 {path}.tmp 再 os.replace, 避免中断留下半写/截断文件。

    原 `pickle.dump(d, open(p,"wb"))` 打开即截断原文件, 进程中断(本机 QMT 多天劣化
    0xC0000409 崩溃为常态)会留下半写 pkl; 且 _is_pkl_fresh 只看 mtime 把坏文件判为
    今日新鲜跳过重建, load 处崩溃传导全批。原子替换后中断只留 .tmp, 最终文件永不半写。
    (第2轮C2, 与 _write_manifest 同口径)"""
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fp:
        pickle.dump(obj, fp)
    os.replace(tmp, path)


def _load_pkl_or_none(path: str):
    """读 pkl, 损坏/截断(EOFError/UnpicklingError/OSError 等)返回 None 而非抛出。

    patch_* 批处理原本 pickle.load 在 try 块外, 扫到任一坏文件即整批崩死; 改为逐文件
    容错跳过(调用方据 None 计 fail 并 continue), 坏文件由下次全量重建自愈。(第2轮C2)"""
    try:
        with open(path, "rb") as fp:
            return pickle.load(fp)
    except Exception:
        return None


def _is_pkl_fresh(path: str) -> bool:
    """F-HIGH-1(audit/_round6_select_data.md):判断已有 pkl 是否「今日已构建」(新鲜)。

    旧逻辑 `os.path.exists(p) → skip` 致已有标的的缓存**永不刷新**,selector 每天读的
    是构建那一刻(可能 N 天前)的 dates[-1]/small_by_bar[last_idx],选股池随 live 进程
    运行与盘面脱节。改为按新鲜度增量刷新:pkl 缺失或文件 mtime 早于今日 0 点(说明不是
    今天重建的)→ 重建;今天已构建过 → 跳过。这样 pre-market cron 每日重跑 fetch 能刷新
    陈旧 pkl,又不在同一天内重复全量重拉。简化口径(mtime 跨天)无交易日历依赖、确定、
    与「pre-market cron 每日刷新一次」运维意图精确对齐。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    today_start = _dt.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    return mtime >= today_start


def _manifest_path(out_dir: str, manifest_path: str | None = None) -> str:
    return manifest_path or os.path.join(out_dir, "_build_manifest.json")


def _write_manifest(path: str, manifest: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _manifest_entry(code: str, status: str, path: str, **extra) -> dict:
    item = {
        "code": code,
        "status": status,
        "path": path,
        "time": _now_iso(),
    }
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


def run(codes, out_dir, small_tf, big_tf, start, end, big_delay, min_small=200,
        mid_tf=None, mid_delay=None, manifest_path=None, manifest_label=None,
        metadata=None, exchange=None):
    os.makedirs(out_dir, exist_ok=True)
    if exchange is None:
        from chanlun.exchange.exchange_qmt import ExchangeQMT
        ex = ExchangeQMT()
    else:
        ex = exchange
    ok = skip = fail = 0
    t0 = time.time()
    lv = f"{small_tf}+{mid_tf}+{big_tf}" if mid_tf else f"{small_tf}+{big_tf}"
    manifest_file = _manifest_path(out_dir, manifest_path)
    manifest = {
        "version": 1,
        "label": manifest_label or "",
        "started_at": _now_iso(),
        "completed_at": None,
        "out_dir": out_dir,
        "levels": {
            "small_tf": small_tf,
            "mid_tf": mid_tf,
            "big_tf": big_tf,
        },
        "start": str(start) if start is not None else None,
        "end": str(end) if end is not None else None,
        "min_small": int(min_small),
        "metadata": dict(metadata or {}),
        "requested_count": len(codes),
        "counts": {"ok": 0, "skip": 0, "fail": 0},
        "entries": [],
    }
    _write_manifest(manifest_file, manifest)
    print(f"取数 {len(codes)}只 {lv} {start}~{end} → {out_dir}")
    for i, code in enumerate(codes):
        p = f"{out_dir}/{code}.pkl"
        # F-HIGH-1:按新鲜度增量刷新(详见 _is_pkl_fresh)。今日已构建的 pkl 跳过,
        # 陈旧/缺失则重建。⚠运维项:本逻辑只在「fetch 被重跑」时才生效——要让 selector
        # 读到当日数据,需把本模块的 run()(+ patch_trend/patch_daily_bsp)挂进每日
        # pre-market cron(类似 ops 里的 QMT 每日重启)。代码侧已支持增量刷新,但不会
        # 自动盘中重跑;不配 cron 则缓存仍停在最后一次手动 fetch 的那天。
        if _is_pkl_fresh(p):
            skip += 1
            manifest["entries"].append(
                _manifest_entry(code, "skip", p, reason="fresh_today")
            )
            manifest["counts"] = {"ok": ok, "skip": skip, "fail": fail}
            continue
        try:
            d = build(code, ex, small_tf, big_tf, start, end, big_delay, min_small,
                      mid_tf=mid_tf, mid_delay=mid_delay)
            if d:
                _atomic_pickle_dump(d, p)
                ok += 1
                manifest["entries"].append(
                    _manifest_entry(
                        code,
                        "ok",
                        p,
                        n_small=d.get("n_small"),
                        n_mid=d.get("n_mid"),
                        n_big=d.get("n_big"),
                        has_mid_by_bar="mid_by_bar" in d,
                    )
                )
            else:
                fail += 1
                manifest["entries"].append(
                    _manifest_entry(code, "fail", p, reason="insufficient_data")
                )
        except Exception as e:
            fail += 1
            print(f"  {code} 失败: {type(e).__name__} {e}")
            manifest["entries"].append(
                _manifest_entry(
                    code,
                    "fail",
                    p,
                    error_type=type(e).__name__,
                    error=str(e),
                )
            )
        manifest["counts"] = {"ok": ok, "skip": skip, "fail": fail}
        if (i + 1) % 30 == 0:
            _write_manifest(manifest_file, manifest)
            print(f"  {i+1}/{len(codes)} ok={ok} skip={skip} fail={fail} {time.time()-t0:.0f}s")
    manifest["completed_at"] = _now_iso()
    manifest["elapsed_seconds"] = round(time.time() - t0, 3)
    manifest["counts"] = {"ok": ok, "skip": skip, "fail": fail}
    _write_manifest(manifest_file, manifest)
    print(f"完成 ok={ok} skip={skip} fail={fail} 共{len(codes)} {time.time()-t0:.0f}s manifest={manifest_file}")
    return manifest


def patch_trend(out_dir: str, big_tf: str, start, end):
    """给已有缓存补「大级别走势方向」(big_trend_events):周线买卖点=0 时门控的替代。

    walk-forward 口径(wf_dir_series,逐根增量重算「当时可见的最后一笔方向」):实盘可复制、
    无 lookahead。旧「最终序列笔事件+delay」口径已被截断实证废弃(滞后中位9bar,固定delay两头错)。"""
    import glob
    from chanlun.exchange.exchange_qmt import ExchangeQMT
    from chanlun.recursive_bt.engine.engine import wf_dir_series
    ex = ExchangeQMT()
    files = sorted(glob.glob(f"{out_dir}/*.pkl"))
    ok = skip = fail = 0
    t0 = time.time()
    print(f"补走势方向(walk-forward) {len(files)}只 {big_tf} → {out_dir}")
    for i, p in enumerate(files):
        d = _load_pkl_or_none(p)
        if d is None:
            fail += 1
            continue
        if d.get("trend_mode") == "wf":
            skip += 1
            continue
        try:
            df = _slice(ex.klines(d["code"], big_tf, start_date=start), start, end)
            ev = []
            if df is not None and len(df) >= 30:
                ev = wf_dir_series(df, d["code"], big_tf)
            d["big_trend_events"] = ev
            d["trend_mode"] = "wf"
            _atomic_pickle_dump(d, p)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  {d.get('code', p)} 失败: {type(e).__name__} {e}")
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(files)} ok={ok} skip={skip} fail={fail} {time.time()-t0:.0f}s")
    print(f"完成 ok={ok} skip={skip} fail={fail} {time.time()-t0:.0f}s")


def patch_daily_bsp(out_dir: str):
    """给主线缓存补日线买卖点信号(三级共振选股锚)。
    QMT 日线全历史 → CL → get_branch_bspoints,存 pkl['daily_bsp']=[(date,bs_type)]。
    口径:anchor_fx.k.date=确认bar收盘(15:00),回测端次日生效(无lookahead,与daily段同源)。"""
    import glob
    from chanlun.exchange.exchange_qmt import ExchangeQMT
    ex = ExchangeQMT()
    files = sorted(glob.glob(f"{out_dir}/*.pkl"))
    ok = skip = fail = 0
    t0 = time.time()
    print(f"补日线买卖点 {len(files)}只 → {out_dir}")
    for i, p in enumerate(files):
        d = _load_pkl_or_none(p)
        if d is None:
            fail += 1
            continue
        if "daily_bsp" in d:
            skip += 1
            continue
        try:
            df = ex.klines(d["code"], "d")
            ev = []
            if df is not None and len(df) >= 100:
                cd = CL(d["code"], "d", dict(CL_CFG))
                cd.process_klines(df)
                ev = [(s.date, s.bs_type) for s in collect_branch_signals(cd, use_xd=False)]
            d["daily_bsp"] = ev
            _atomic_pickle_dump(d, p)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  {d.get('code', p)} 失败: {type(e).__name__} {e}")
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(files)} ok={ok} skip={skip} fail={fail} {time.time()-t0:.0f}s")
    print(f"完成 ok={ok} skip={skip} fail={fail} {time.time()-t0:.0f}s")


def patch_nest_annotations(
    out_dir: str,
    small_tf: str = "5m",
    big_tf: str = "30m",
    start=None,
    end=None,
    big_delay=pd.Timedelta("30min"),
    mid_tf=None,
    mid_delay=None,
    limit: int | None = None,
    codes: set[str] | None = None,
):
    """Rebuild technical signal cache so 1/2 buy points carry interval-nest flags."""
    import glob
    from chanlun.exchange.exchange_qmt import ExchangeQMT

    ex = ExchangeQMT()
    files = sorted(glob.glob(f"{out_dir}/*.pkl"))
    if codes:
        files = [p for p in files if os.path.basename(p)[:-4] in codes]
    ok = skip = fail = 0
    t0 = time.time()
    print(f"补区间套标注 {len(files)}只 {small_tf}+{big_tf} → {out_dir}")
    technical_keys = {
        "dates", "open", "close", "small_by_bar", "big_dir_at", "mid_dir_at",
        "mid_by_bar", "limit_pct", "n_small", "n_big", "n_mid", "code", "signal_schema",
    }
    for i, p in enumerate(files):
        d = _load_pkl_or_none(p)
        if d is None:
            fail += 1
            continue
        schema = d.get("signal_schema") or {}
        if schema.get("nest_operable") and int(schema.get("nest_key") or 0) >= SIGNAL_SCHEMA["nest_key"]:
            skip += 1
            continue
        try:
            rebuilt = build(
                d["code"], ex, small_tf, big_tf, start, end, big_delay,
                mid_tf=mid_tf, mid_delay=mid_delay,
            )
            if not rebuilt:
                fail += 1
                continue
            for k, v in d.items():
                if k not in technical_keys:
                    rebuilt[k] = v
            _atomic_pickle_dump(rebuilt, p)
            ok += 1
            if limit is not None and ok >= limit:
                break
        except Exception as e:
            fail += 1
            print(f"  {d.get('code', p)} 失败: {type(e).__name__} {e}")
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(files)} ok={ok} skip={skip} fail={fail} {time.time()-t0:.0f}s")
    print(f"完成 ok={ok} skip={skip} fail={fail} {time.time()-t0:.0f}s")


def build_st_list(out_path: str, codes=None, detail_fn=None) -> dict:
    """构建主板 ST/*ST 名单 `_st_list.json`({code: name},±5% 涨跌停)。
    创业板/科创板 ST 涨跌幅仍 20%、北交所无 ST 制度,均不入名单。
    按当前名称近似(戴帽/摘帽时点未追溯)。"""
    from chanlun.recursive_bt.engine.market_runtime import ashare_board

    if detail_fn is None:
        from xtquant import xtdata

        def detail_fn(code):
            num, mkt = code.split(".")[1], code.split(".")[0]
            return xtdata.get_instrument_detail(f"{num}.{mkt}") or {}

    if codes is None:
        codes = universe_all_a()
    out = {}
    for code in codes:
        if ashare_board(code) != "main":
            continue
        try:
            name = str((detail_fn(code) or {}).get("InstrumentName") or "")
        except Exception:
            continue
        if "ST" in name.upper():
            out[code] = name
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    print(f"st_list: {len(out)} codes -> {out_path}")
    return out


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "st_list":
        build_st_list(f"{OUT_ALL_A}/_st_list.json")
        return
    if arg == "nest_all_a":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        patch_nest_annotations(OUT_ALL_A, limit=limit)
        return
    if arg == "nest_codes_all_a":
        codes = {
            item.strip().upper()
            for item in (sys.argv[2] if len(sys.argv) > 2 else "").replace(";", ",").split(",")
            if item.strip()
        }
        patch_nest_annotations(OUT_ALL_A, codes=codes)
        return
    if arg == "nest":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        patch_nest_annotations(OUT, limit=limit)
        return
    if arg == "daily_trend":
        patch_trend(OUT_DAILY, "w", "2022-01-01", "2024-12-31")
        return
    if arg == "trend_all_a":
        patch_trend(OUT_ALL_A, "30m", None, None)
        return
    if arg == "daily_bsp_all_a":
        patch_daily_bsp(OUT_ALL_A)
        return
    if arg == "trend":
        patch_trend(OUT, "30m", None, None)
        return
    if arg == "daily_bsp":
        patch_daily_bsp(OUT)
        return
    if arg == "daily":
        codes = universe("沪深300") + ["SH.000001"]
        run(codes, OUT_DAILY, "d", "w", "2022-01-01", "2024-12-31",
            big_delay=pd.Timedelta("7D"), min_small=120)
        return
    if arg == "mtf3":
        sector = sys.argv[2] if len(sys.argv) > 2 else "上证50"
        codes = universe(sector) + ["SH.000001"]
        run(codes, OUT_MTF3, "1m", "30m", None, None, big_delay=pd.Timedelta("30min"),
            min_small=500, mid_tf="5m", mid_delay=pd.Timedelta("5min"),
            manifest_label="mtf3",
            metadata={"sector": sector})
        return
    if arg == "mtf3_all_a":
        limit, board_filter, sample_mode = _parse_limit_board_sample(sys.argv[2:])
        codes = filter_codes_by_board(universe_all_a(None), board_filter)
        codes = sample_codes_by_board(codes, limit, sample_mode)
        codes = codes + ["SH.000001"]
        run(codes, OUT_MTF3_ALL_A, "1m", "30m", None, None,
            big_delay=pd.Timedelta("30min"), min_small=500,
            mid_tf="5m", mid_delay=pd.Timedelta("5min"),
            manifest_label="mtf3_all_a",
            metadata={
                "limit": limit,
                "board_filter": board_filter or "all",
                "sample_mode": sample_mode,
            })
        return

    sector = arg or "上证50"
    limit, board_filter = _parse_limit_and_board(sys.argv[2:])
    if sector in ALL_A_ALIASES:
        codes = filter_codes_by_board(universe_all_a(None), board_filter)
        if limit:
            codes = codes[:limit]
        codes = codes + ["SH.000001"]
        out_dir = OUT_ALL_A
    else:
        codes = universe(sector, limit)
        out_dir = OUT
    run(codes, out_dir, "5m", "30m", None, None,
        big_delay=pd.Timedelta("30min"))


if __name__ == "__main__":
    main()
