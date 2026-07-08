"""kline_recompute 持久 CL 池(增量重算)：复用判定 + 增量==全量对拍。

- 复用判定(mock CL，快)：首根稳定→复用同实例；向左滚动/config变/无cache_key→新建。
- 增量==全量(真实 CL + 合成 K 线)：逐前缀复用(增量)的 cl_data_to_tv_chart 输出与
  每次新建(全量)完全一致——钉死"SSE/轮询走增量复用不改变缠论结果"。
"""
import math

import pandas as pd
import pytest

from cl_app.services import kline_recompute
from cl_app.services.kline_recompute import recompute_chart_data_from_klines


def _klines_df(ts, prices):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(ts), unit="s", utc=True),
            "open": list(prices),
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": list(prices),
            "volume": [1000] * len(ts),
        }
    )


# ── 层 1：复用判定(mock CL) ──────────────────────────────────────────
class _FakeCL:
    instances = []

    def __init__(self, *a, **k):
        _FakeCL.instances.append(self)
        self.n = 0

    def process_klines(self, klines):
        self.n = len(klines)


@pytest.fixture
def mock_cl(monkeypatch):
    _FakeCL.instances = []
    monkeypatch.setattr("chanlun.core.cl.CL", _FakeCL)
    monkeypatch.setattr(
        "chanlun.cl_utils.cl_data_to_tv_chart",
        lambda cd, cfg, to_frequency=None: {"n": cd.n, "id": id(cd)},
    )
    kline_recompute.reset_cl_pool()
    yield
    kline_recompute.reset_cl_pool()


def test_reuse_when_prefix_stable(mock_cl):
    """首根 date 不变 + 根数不减 → 复用同一 CL(增量)。"""
    r1 = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]), cache_key="a:SYN:1m"
    )
    r2 = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060, 1120], [10, 11, 12]), cache_key="a:SYN:1m"
    )
    # 不数全局 _FakeCL.instances(前序测试残留的 prewarm/SSE 后台线程会在 patch 窗口内构造
    # CL 污染计数, 曾致约 1/7 间歇失败, 同 test_no_reuse_without_cache_key)。id 相同已证复用。
    assert r1["id"] == r2["id"]  # 同一 CL 实例(增量复用)


def test_new_cl_when_first_date_changes(mock_cl):
    """首根 date 变早(向左滚动)→ 新建 CL(全量)。"""
    recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]), cache_key="a:SYN:1m"
    )
    recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([940, 1000, 1060], [9, 10, 11]), cache_key="a:SYN:1m"
    )
    assert len(_FakeCL.instances) == 2  # 向左滚动新建


def test_new_cl_when_config_changes(mock_cl):
    """cl_config 变 → 不可复用, 新建 CL。"""
    recompute_chart_data_from_klines(
        "a", "SYN", "1m", {"fx_bh": "yes"}, _klines_df([1000, 1060], [10, 11]), cache_key="a:SYN:1m"
    )
    recompute_chart_data_from_klines(
        "a", "SYN", "1m", {"fx_bh": "no"}, _klines_df([1000, 1060, 1120], [10, 11, 12]), cache_key="a:SYN:1m"
    )
    assert len(_FakeCL.instances) == 2


def test_no_reuse_without_cache_key(mock_cl):
    """无 cache_key → 每次新建(不入池), 向后兼容全量行为。

    断言用两次返回的实例 id 不同(各自新建),不数全局 _FakeCL.instances——patch 窗口内
    前序测试残留的后台线程(prewarm/SSE 重算)构造 CL 会污染全局计数,曾致 ~1/7 间歇失败。
    """
    r1 = recompute_chart_data_from_klines("a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]))
    r2 = recompute_chart_data_from_klines("a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]))
    assert r1["id"] != r2["id"]  # 各自新建、不共享实例


def test_store_cl_to_pool_then_reuse(mock_cl):
    """store_cl_to_pool 存入的实例, 后续 recompute(同 cache_key) 命中增量复用。

    模拟"首次加载 web_batch 算好 CL → 存池 → 第一次轮询走 inc 而非 full(消 52s)"。
    """
    ext_cl = _FakeCL()  # 模拟首次加载外部算好的 CL
    base = _klines_df([1000, 1060], [10, 11])
    kline_recompute.store_cl_to_pool("a:SYN:1m", ext_cl, base, {}, "a")
    # 第一次轮询: 末尾追加新根、同 cache_key → 应复用 ext_cl(不新建)
    nxt = _klines_df([1000, 1060, 1120], [10, 11, 12])
    r = recompute_chart_data_from_klines("a", "SYN", "1m", {}, nxt, cache_key="a:SYN:1m")
    assert len(_FakeCL.instances) == 1  # 只有 store 的那个, recompute 未再新建(=走了增量)
    # T0-1: 池存独立副本(deepcopy)斩断别名 → 复用的是副本而非 ext_cl 本身;
    # 功能等价由"未新建 + n 正确(副本走增量处理了 3 根)"保证, 不再断言 id 相同(那是别名 bug)。
    assert r["n"] == 3


def test_store_cl_to_pool_breaks_alias(mock_cl):
    """T0-1: store_cl_to_pool 存入池的必须是独立副本(deepcopy), 不与传入实例共享引用。

    传入的 cl 可能是 cl_object_cache 的共享实例;若按引用存池, _cl_pool 路径(chart_calc_locks)
    与 cl_object_cache 路径(_key_locks)会在两套不相交锁下并发 mutate 同一 CL → 增量状态机
    撕裂 → 错误缠论/买卖点。存独立副本斩断别名。
    """
    ext_cl = _FakeCL()
    base = _klines_df([1000, 1060], [10, 11])
    kline_recompute.store_cl_to_pool("a:SYN:1m", ext_cl, base, {}, "a")
    pooled = kline_recompute._cl_pool["a:SYN:1m"]["cl"]
    assert pooled is not ext_cl, "池里必须是独立副本, 不能是传入实例的别名(T0-1)"


def test_store_real_cl_deepcopy_preserves_increment():
    """T0-1 修复正确性(真实 CL): store_cl_to_pool 对真实 CL 深拷后, 副本继续增量重算
    应与全量新建完全一致——验证 deepcopy 不抛、保真(增量状态完整)、斩断别名后功能不变。
    """
    from chanlun.core.cl import CL
    kline_recompute.reset_cl_pool()
    cfg = _cl_config()
    klines = _synth_klines(200)
    base = klines.iloc[:180].copy()
    ext = klines.iloc[:186].copy()  # 末尾追加 6 根, 前 180 根与 base 一致(前缀稳定)
    ck = "a:SYNSTORE:1m"
    try:
        # 模拟 firstload: 真实 CL 算好 base(180 根)
        cl = CL("SYNSTORE", "1m", dict(cfg), market="a")
        cl.process_klines(base.copy())
        # store: 触发对真实 CL 的 deepcopy(不应抛); 池里是独立副本
        kline_recompute.store_cl_to_pool(ck, cl, base, cfg, "a")
        assert kline_recompute._cl_pool[ck]["cl"] is not cl, "真实 CL 也须存独立副本"
        # recompute 复用池副本走增量(180→186)
        inc = recompute_chart_data_from_klines("a", "SYNSTORE", "1m", cfg, ext.copy(), cache_key=ck)
        # 全量对照(无 cache_key, 新建空 CL 全量算 186)
        full = recompute_chart_data_from_klines("a", "SYNSTORE", "1m", cfg, ext.copy())
        assert inc == full, "deepcopy 副本增量重算结果 != 全量"
    finally:
        kline_recompute.reset_cl_pool()


def test_store_cl_to_pool_deepcopy_failure_degrades():
    """T0-1 修复健壮性: deepcopy 若抛(并发渲染改共享 O1 致 dict size change), store 应降级——
    跳过入池(不抛、不入池), 由下次 recompute 走 full, 而非把异常冒泡拖垮用户首屏请求。
    """
    kline_recompute.reset_cl_pool()

    class _BoomCL:
        def __deepcopy__(self, memo):
            raise RuntimeError("dictionary changed size during iteration")

    base = _klines_df([1000, 1060], [10, 11])
    # 不应抛(降级吞掉 deepcopy 异常); 用 __deepcopy__ 抛, 只影响本对象不误伤 pandas
    kline_recompute.store_cl_to_pool("a:SYN:1m", _BoomCL(), base, {}, "a")
    # 未入池 → 下次 recompute 走 full(正确, 仅慢一次)
    assert "a:SYN:1m" not in kline_recompute._cl_pool


# ── 层 2：增量 == 全量(真实 CL + 合成 K 线) ──────────────────────────
def _synth_klines(n, start_ts=1_600_000_000):
    """确定性合成 K 线(多周期正弦叠加 → 趋势+转折, 产生分型/笔/线段/中枢)。"""
    rows = []
    for i in range(n):
        v = 100 + 18 * math.sin(i / 17.0) + 7 * math.sin(i / 5.0) + 2.5 * math.sin(i / 2.0)
        vn = 100 + 18 * math.sin((i + 0.5) / 17.0) + 7 * math.sin((i + 0.5) / 5.0) + 2.5 * math.sin((i + 0.5) / 2.0)
        o, c = v, vn
        rows.append(
            {
                "date": pd.Timestamp(start_ts + i * 60, unit="s", tz="UTC"),
                "open": o,
                "high": max(o, c) + 0.5,
                "low": min(o, c) - 0.5,
                "close": c,
                "volume": 1000 + i,
            }
        )
    return pd.DataFrame(rows)


def _cl_config():
    try:
        from chanlun.cl_utils import query_cl_chart_config
        return query_cl_chart_config("a", "SYNINC")
    except Exception:
        return {}


def test_incremental_equals_full_end_to_end():
    """真实 CL: 逐前缀复用(增量)的输出与每次新建(全量)完全一致。"""
    kline_recompute.reset_cl_pool()
    klines = _synth_klines(360)
    cfg = _cl_config()
    ck = "a:SYNINC:1m"
    try:
        for i in range(60, 361, 6):
            prefix = klines.iloc[:i].copy()
            full = recompute_chart_data_from_klines("a", "SYNINC", "1m", cfg, prefix.copy())
            inc = recompute_chart_data_from_klines("a", "SYNINC", "1m", cfg, prefix.copy(), cache_key=ck)
            assert inc == full, f"前缀 {i}: 增量 != 全量"
    finally:
        kline_recompute.reset_cl_pool()


def test_incremental_equals_full_on_mid_bar_revision():
    """中间根被修订(非末根, 根数不变)→ 复用增量 应 == 新建全量。

    审计 H1 指出:增量路径(持久 CL 池)复用判据只看 config/首根/根数,不校验中间根;
    持久 CL 的 process_klines 会把 date<末根 的传入帧裁掉 → 若数据源回填/订正中间某根,
    增量会丢弃该变更, 而全量(新建空 CL)会用修订后序列重算。本用例实证这一分歧是否真实。
    """
    kline_recompute.reset_cl_pool()
    klines = _synth_klines(200)
    cfg = _cl_config()
    ck = "a:SYNMID:1m"
    try:
        base = klines.iloc[:200].copy()
        recompute_chart_data_from_klines("a", "SYNMID", "1m", cfg, base.copy(), cache_key=ck)  # 建基线入池
        # 修订中间某根(index 100)OHLC, 根数与末根都不变
        rev = base.copy()
        ic = rev.columns.get_loc("close")
        ih = rev.columns.get_loc("high")
        il = rev.columns.get_loc("low")
        rev.iloc[100, ic] = rev.iloc[100, ic] + 5.0
        rev.iloc[100, ih] = rev.iloc[100, ih] + 5.0
        rev.iloc[100, il] = rev.iloc[100, il] - 5.0
        full = recompute_chart_data_from_klines("a", "SYNMID", "1m", cfg, rev.copy())
        inc = recompute_chart_data_from_klines("a", "SYNMID", "1m", cfg, rev.copy(), cache_key=ck)
        assert inc == full, "中间根修订: 增量 != 全量(坐实审计 H1:复用判据漏判中间根)"
    finally:
        kline_recompute.reset_cl_pool()


def test_incremental_equals_full_on_last_bar_update():
    """末根 OHLC 更新(盘中同根刷新, 根数不变)→ 复用增量 == 新建全量。

    这是 SSE 最高频场景: 同一分钟内末根价格随 tick 变动。复用 CL 时根数不变、
    first_date 不变 → 命中复用, CL 须按末根变化做增量并与全量一致。
    """
    kline_recompute.reset_cl_pool()
    klines = _synth_klines(200)
    cfg = _cl_config()
    ck = "a:SYNUPD:1m"
    try:
        base = klines.iloc[:200].copy()
        recompute_chart_data_from_klines("a", "SYNUPD", "1m", cfg, base.copy(), cache_key=ck)  # 建基线
        # 末根 OHLC 上抬(模拟盘中拉升, 根数不变)
        upd = base.copy()
        ic, ih = upd.columns.get_loc("close"), upd.columns.get_loc("high")
        upd.iloc[-1, ic] = upd.iloc[-1, ic] + 3.0
        upd.iloc[-1, ih] = upd.iloc[-1, ih] + 3.0
        full = recompute_chart_data_from_klines("a", "SYNUPD", "1m", cfg, upd.copy())
        inc = recompute_chart_data_from_klines("a", "SYNUPD", "1m", cfg, upd.copy(), cache_key=ck)
        assert inc == full, "末根更新: 增量 != 全量"
    finally:
        kline_recompute.reset_cl_pool()
