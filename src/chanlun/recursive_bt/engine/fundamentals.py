"""QMT 基本面+市值数据抓取模块,供「三个独立系统」之①基本面、②比价使用。

①基本面=行业地位(龙头)+质量(ROE/毛利)+成长(营收/净利增长);
②比价=市值与行业地位关系、低估(PB/PE)。本模块抓 PershareIndex(每股指标,**带 m_anntime 公告日
→ point-in-time 防 lookahead**) + 总股本(市值=总股本×价),缓存到 bt_data_fund/{code}.pkl。
运行: PYTHONPATH="src;web/chanlun_chart;." python -m chanlun.recursive_bt.engine.fundamentals [沪深300]
"""
from __future__ import annotations

import glob
import os
import pickle
import sys
import tempfile
import time


def _atomic_dump(obj, path):
    """原子写 pickle: 写临时文件后 os.replace, 防进程中断留半写/截断文件(R5)。"""
    _d = os.path.dirname(path) or "."
    os.makedirs(_d, exist_ok=True)
    _fd, _tmp = tempfile.mkstemp(dir=_d, suffix=".tmp")
    try:
        with os.fdopen(_fd, "wb") as _fh:
            pickle.dump(obj, _fh)
        os.replace(_tmp, path)
    except Exception:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise


OUT = "D:/chanlun_pro/bt_data_fund"
OUT_ALL_A = "D:/chanlun_pro/bt_data_fund_all_a"
ALL_A_ALIASES = {"全A股", "全A", "all_a", "alla", "all"}
ALL_A_SECTORS = ("沪深A股", "沪深京A股", "上证A股", "深证A股", "北证A股")
BOARD_ALIASES = {
    "all": None,
    "shsz": {"main", "gem", "star"},
    "non_bj": {"main", "gem", "star"},
    "non-bj": {"main", "gem", "star"},
}
BOARD_ORDER = ("main", "gem", "star", "bj")


def to_qmt(code: str) -> str:        # 'SH.600519' → '600519.SH'
    mkt, num = code.split(".")
    return f"{num}.{mkt}"


def fetch(codes, out_dir=OUT):
    from xtquant import xtdata
    os.makedirs(out_dir, exist_ok=True)
    qcodes = [to_qmt(c) for c in codes]
    print(f"下载 {len(codes)} 只财务(PershareIndex)...")
    xtdata.download_financial_data(qcodes, ["PershareIndex"])
    print("下载完成,逐只抽取+缓存")
    ok = fail = 0
    t0 = time.time()
    for i, (code, qc) in enumerate(zip(codes, qcodes)):
        p = f"{out_dir}/{code}.pkl"
        if os.path.exists(p):
            ok += 1
            continue
        try:
            fd = xtdata.get_financial_data([qc], ["PershareIndex"],
                                           "20210101", "20260601", report_type="report_time")
            pi = fd.get(qc, {}).get("PershareIndex")
            det = xtdata.get_instrument_detail(qc)
            reports = []
            if pi is not None and len(pi):
                for _, r in pi.iterrows():
                    ann = str(r.get("m_anntime") or "")
                    if not ann or ann == "nan":
                        continue
                    reports.append({
                        "anntime": ann,                          # 公告日 YYYYMMDD
                        "period": str(r.get("m_timetag") or ""),
                        "roe": _f(r.get("du_return_on_equity")),   # 单季ROE %
                        "gross": _f(r.get("sales_gross_profit")),  # 毛利率 %
                        "rev_inc": _f(r.get("inc_revenue_rate")),  # 营收同比增 %
                        "np_inc": _f(r.get("inc_net_profit_rate")),  # 净利同比增 %
                        "bps": _f(r.get("s_fa_bps")),              # 每股净资产
                        "eps": _f(r.get("s_fa_eps_basic")),        # 每股收益(单期)
                    })
            reports.sort(key=lambda x: x["anntime"])
            d = {"code": code, "reports": reports,
                 "total_share": _f(det.get("TotalVolume")) if det else None}
            _atomic_dump(d, p)
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  {code} 失败 {type(e).__name__} {e}")
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(codes)} ok={ok} fail={fail} {time.time()-t0:.0f}s")
    print(f"完成 ok={ok} fail={fail} {time.time()-t0:.0f}s → {out_dir}")


def fetch_batched(codes, out_dir=OUT, batch_size=50):
    """Fetch QMT financial data in small batches to avoid spawning storms."""
    from xtquant import xtdata
    os.makedirs(out_dir, exist_ok=True)
    pending = [code for code in codes if not os.path.exists(f"{out_dir}/{code}.pkl")]
    ok = len(codes) - len(pending)
    fail = 0
    t0 = time.time()
    print(
        f"download {len(codes)} financial symbols, "
        f"pending={len(pending)}, batch_size={batch_size}",
        flush=True,
    )
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        qcodes = [to_qmt(c) for c in batch]
        try:
            xtdata.download_financial_data(qcodes, ["PershareIndex"])
        except Exception as exc:
            print(
                f"  batch {start+1}-{start+len(batch)} download failed "
                f"{type(exc).__name__} {exc}",
                flush=True,
            )
        for code, qc in zip(batch, qcodes):
            p = f"{out_dir}/{code}.pkl"
            try:
                fd = xtdata.get_financial_data(
                    [qc], ["PershareIndex"],
                    "20210101", "20260601", report_type="report_time",
                )
                pi = fd.get(qc, {}).get("PershareIndex")
                det = xtdata.get_instrument_detail(qc)
                reports = []
                if pi is not None and len(pi):
                    for _, r in pi.iterrows():
                        ann = str(r.get("m_anntime") or "")
                        if not ann or ann == "nan":
                            continue
                        reports.append({
                            "anntime": ann,
                            "period": str(r.get("m_timetag") or ""),
                            "roe": _f(r.get("du_return_on_equity")),
                            "gross": _f(r.get("sales_gross_profit")),
                            "rev_inc": _f(r.get("inc_revenue_rate")),
                            "np_inc": _f(r.get("inc_net_profit_rate")),
                            "bps": _f(r.get("s_fa_bps")),
                            "eps": _f(r.get("s_fa_eps_basic")),
                        })
                reports.sort(key=lambda x: x["anntime"])
                d = {
                    "code": code,
                    "reports": reports,
                    "total_share": _f(det.get("TotalVolume")) if det else None,
                }
                _atomic_dump(d, p)
                ok += 1
            except Exception as exc:
                fail += 1
                print(f"  {code} failed {type(exc).__name__} {exc}", flush=True)
        print(
            f"  {min(start+len(batch), len(pending))}/{len(pending)} "
            f"pending ok={ok} fail={fail} {time.time()-t0:.0f}s",
            flush=True,
        )
    print(f"done ok={ok} fail={fail} {time.time()-t0:.0f}s -> {out_dir}", flush=True)


def _f(v):
    try:
        f = float(v)
        return f if f == f else None      # NaN → None
    except (TypeError, ValueError):
        return None


def universe(sector):
    from xtquant import xtdata
    xtdata.download_sector_data()
    codes = xtdata.get_stock_list_in_sector(sector) or []
    return sorted(f"{c.split('.')[1]}.{c.split('.')[0]}" for c in codes)


def universe_all_a():
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
    return sorted(out)


def ashare_board(code: str) -> str:
    code = (code or "").strip().upper()
    if code.startswith("BJ."):
        return "bj"
    num = code.split(".")[1]
    if num.startswith("8") or num.startswith("920"):
        return "bj"
    if num.startswith(("688", "689")):
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


def main():
    sector = sys.argv[1] if len(sys.argv) > 1 else None
    out_dir = OUT
    if sector in ALL_A_ALIASES:
        limit, board_filter = _parse_limit_and_board(sys.argv[2:])
        codes = filter_codes_by_board(universe_all_a(), board_filter)
        if limit:
            codes = codes[:limit]
        out_dir = OUT_ALL_A
    elif sector:
        codes = universe(sector)
    else:   # 默认:已有 K线缓存的标的(bt_data)
        codes = sorted(os.path.basename(f)[:-4]
                       for f in glob.glob("D:/chanlun_pro/bt_data/*.pkl"))
    fetch_batched(codes, out_dir) if sector in ALL_A_ALIASES else fetch(codes, out_dir)


if __name__ == "__main__":
    main()
