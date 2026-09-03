"""集成测试: tv_history 端点对「过期全量快照」走 serve-stale(秒返)+ 派后台重验证。

直接打 /tv/history 端点验证整条路径(路由→鉴权→evaluate→serve-stale→序列化):
- 不连数据源: monkeypatch ``market_now_trading``(避免构造 exchange)+
  ``submit_revalidation``(记录是否触发, 不真重算);
- 用 RAM 预置一个过期全量快照, 验证端点立即返回旧数据 + 触发后台刷新;
- 对照: 新鲜快照不触发刷新。
"""
import pathlib
import sys
import time

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import pytest  # noqa: E402

from cl_app import create_app  # noqa: E402
from cl_app.blueprints import tv as tv_mod  # noqa: E402
from cl_app.services import chart_cache  # noqa: E402
from chanlun.cl_utils import query_cl_chart_config  # noqa: E402

MARKET, CODE, FREQ = "a", "SH.688182", "5m"


@pytest.fixture
def client():
    app = create_app(test_config={
        "TESTING": True,
        "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
    })
    return app.test_client()


def _make_full_chart_data(n=50):
    t0 = 1_700_000_000  # 2023-11, 远早于 to → 不会被 trim_future_bars 裁掉
    t = [t0 + i * 300 for i in range(n)]
    one = [1.0] * n
    return {"t": t, "o": one, "h": one, "l": one, "c": one, "v": one}


def _seed_entry(validated_at):
    cfg = query_cl_chart_config(MARKET, CODE)
    key = chart_cache._build_cache_key(MARKET, CODE, FREQ, cfg)
    entry = chart_cache._build_chart_cache_entry(
        _make_full_chart_data(50), is_full_snapshot=True, validated_at=validated_at
    )
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache[key] = entry
    return key


def _url():
    to = int(time.time()) + 86400
    return f"/tv/history?symbol={MARKET}:{CODE}&resolution=5&firstDataRequest=true&from=0&to={to}"


def test_stale_snapshot_serves_stale_and_revalidates(client, monkeypatch):
    monkeypatch.setattr(tv_mod, "market_now_trading", lambda m: False)
    called = []
    monkeypatch.setattr(tv_mod, "submit_revalidation", lambda *a, **k: called.append(a))
    monkeypatch.setattr(
        tv_mod.market_frequencys,
        "get",
        lambda *_args, **_kwargs: pytest.fail(
            "cache hits must not synchronously load market metadata"
        ),
    )
    _seed_entry(validated_at=time.time() - 9999)  # 过期(>收盘阈值 3600)

    t0 = time.time()
    resp = client.get(_url())
    elapsed_ms = (time.time() - t0) * 1000

    j = resp.get_json()
    assert j["s"] == "ok"
    assert len(j["t"]) == 50           # 立即返回预置的旧快照(秒返)
    assert len(called) == 1            # serve-stale → 触发了一次后台重验证
    assert elapsed_ms < 800            # 没走 ex.klines 同步重算
    print(
        f"\n[serve-stale] elapsed={elapsed_ms:.0f}ms bars={len(j['t'])} "
        f"revalidate_called={len(called)}"
    )


def test_fresh_snapshot_hits_without_revalidate(client, monkeypatch):
    monkeypatch.setattr(tv_mod, "market_now_trading", lambda m: False)
    called = []
    monkeypatch.setattr(tv_mod, "submit_revalidation", lambda *a, **k: called.append(a))
    _seed_entry(validated_at=time.time() - 5)  # 新鲜

    resp = client.get(_url())
    j = resp.get_json()
    assert j["s"] == "ok"
    assert len(j["t"]) == 50
    assert len(called) == 0             # 新鲜 → 不触发后台刷新


def test_endpoint_reads_disk_cache_outside_global_cache_lock(client, monkeypatch):
    monkeypatch.setattr(tv_mod, "market_now_trading", lambda _market: False)
    monkeypatch.setattr(tv_mod, "submit_revalidation", lambda *_a, **_k: None)
    entry = chart_cache._build_chart_cache_entry(
        _make_full_chart_data(10), is_full_snapshot=True, validated_at=time.time()
    )
    observed = []

    def _read_entry(_cache_key):
        is_owned = getattr(chart_cache.cache_lock, "_is_owned", lambda: False)()
        observed.append(is_owned)
        assert is_owned is False, "disk-capable cache read ran inside global cache_lock"
        return entry

    monkeypatch.setattr(tv_mod, "_get_chart_cache_entry", _read_entry)

    response = client.get(_url())

    assert response.status_code == 200
    assert response.get_json()["s"] == "ok"
    assert observed == [False]


def test_complete_cache_hit_never_waits_for_calculation_lock(client, monkeypatch):
    """A readable snapshot stays interactive while background refresh owns its lock."""

    _seed_entry(validated_at=time.time())
    monkeypatch.setattr(tv_mod, "market_now_trading", lambda _market: False)
    monkeypatch.setattr(tv_mod, "submit_revalidation", lambda *_a, **_k: None)

    class _UnexpectedLockRegistry:
        def get(self, _cache_key):
            raise AssertionError("complete cache hits must bypass the calc lock")

    monkeypatch.setattr(tv_mod, "chart_calc_locks", _UnexpectedLockRegistry())

    response = client.get(_url())

    assert response.status_code == 200
    assert response.get_json()["s"] == "ok"


def test_cache_miss_rechecks_after_waiting_for_calculation_lock(client, monkeypatch):
    """A concurrent fill wins while waiting; the follower must not recompute it."""

    cfg = query_cl_chart_config(MARKET, CODE)
    key = chart_cache._build_cache_key(MARKET, CODE, FREQ, cfg)
    filled_entry = chart_cache._build_chart_cache_entry(
        _make_full_chart_data(12), is_full_snapshot=True, validated_at=time.time()
    )
    reads = []

    def _read_entry(_cache_key):
        assert _cache_key == key
        reads.append(_cache_key)
        return None if len(reads) == 1 else filled_entry

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

    class _LockRegistry:
        def get(self, _cache_key):
            assert _cache_key == key
            return _Lock()

    monkeypatch.setattr(tv_mod, "_get_chart_cache_entry", _read_entry)
    monkeypatch.setattr(tv_mod, "chart_calc_locks", _LockRegistry())
    monkeypatch.setattr(tv_mod, "market_now_trading", lambda _market: False)
    monkeypatch.setattr(tv_mod, "submit_revalidation", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tv_mod,
        "fetch_klines_and_compute_cl_data",
        lambda *_a, **_k: pytest.fail("the second cache check should win"),
    )

    response = client.get(_url())

    assert response.status_code == 200
    assert response.get_json()["s"] == "ok"
    assert len(response.get_json()["t"]) == 12
    assert len(reads) == 2


def test_history_before_ram_cache_floor_does_not_wait_for_calculation_lock(
    client, monkeypatch
):
    """Indicator warm-up before a complete embedded history must finish at once.

    A background revalidation can legitimately own the per-symbol calculation
    lock.  The requested range is already known to end before the oldest cached
    bar, so queueing behind that work would reveal a second batch of bars after
    the parent page has declared the chart ready.
    """

    _seed_entry(validated_at=time.time())

    class _UnexpectedLockRegistry:
        def get(self, _cache_key):
            raise AssertionError("cache-floor no-data must bypass the calc lock")

    monkeypatch.setattr(tv_mod, "chart_calc_locks", _UnexpectedLockRegistry())
    monkeypatch.setattr(
        tv_mod,
        "market_now_trading",
        lambda _market: pytest.fail("cache-floor no-data must bypass market work"),
    )
    oldest = _make_full_chart_data(1)["t"][0]

    response = client.get(
        f"/tv/history?symbol={MARKET}:{CODE}&resolution=5"
        f"&firstDataRequest=false&countback=69"
        f"&from={oldest - 7200}&to={oldest}"
    )

    assert response.status_code == 200
    assert response.get_json() == {"s": "no_data"}


def test_partial_cache_entry_is_not_mutated(client, monkeypatch):
    """T0-2: cache hit 缺 higher_macd_* 键时的 lazy 补算, 不得原地给共享 cache dict 加键。

    否则锁外的 trim_future_bars(dict(chart_data) 整体迭代) / SSE {**data} 读同一共享 dict 时,
    锁内加键会撞 'dictionary changed size during iteration' → 请求兜成 no_data + SSE 丢帧。
    修复: lazy 补算作用于浅拷副本 + 整体替换 entry; 原共享 dict 永不被加键。
    """
    import math

    monkeypatch.setattr(tv_mod, "market_now_trading", lambda m: False)
    monkeypatch.setattr(tv_mod, "submit_revalidation", lambda *a, **k: None)

    # 护栏: 走到 cache miss(重算)说明预置没命中、根本没到 lazy 块 → 测试无意义, 直接炸。
    def _no_miss(*a, **k):
        raise AssertionError("expected cache HIT (lazy path), got cache MISS")

    monkeypatch.setattr(tv_mod, "fetch_klines_and_compute_cl_data", _no_miss)

    cfg = query_cl_chart_config(MARKET, CODE)
    key = chart_cache._build_cache_key(MARKET, CODE, FREQ, cfg)
    # 缺 higher_macd_* 的 5m full snapshot; 5m→30m 分桶 = t//1800(每 6 根一桶), apply 要求
    # 桶数 > slow+signal(26+9=35) 才写入 → 需 >210 根 5m。取 400 根(~67 桶)确保 apply 返回 True、
    # 真正走到"加键"那一步(否则 mutation 路径空过)。close 用 sin 有变化, 避免 talib 退化。
    n = 400
    t0 = 1_700_000_000
    t = [t0 + i * 300 for i in range(n)]
    c = [100.0 + 10.0 * math.sin(i / 9.0) for i in range(n)]
    data = {
        "t": t, "o": list(c), "h": [x + 0.5 for x in c],
        "l": [x - 0.5 for x in c], "c": list(c), "v": [1.0] * n,
    }
    entry = chart_cache._build_chart_cache_entry(
        data, is_full_snapshot=True, validated_at=time.time() - 5
    )
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache[key] = entry
    shared = chart_cache.chart_data_cache[key]["data"]
    assert "higher_macd_hist" not in shared  # 前置: 确实缺 HTF 键
    keys_before = set(shared.keys())

    j = client.get(_url()).get_json()
    assert j["s"] == "ok"
    # 核心(T0-2): 原共享 cache dict 不被原地加 higher_macd_* 键(修复: 补算到副本+整体替换 entry)
    assert set(shared.keys()) == keys_before, (
        f"共享 cache dict 被原地加键: {set(shared.keys()) - keys_before}"
    )
    # The current cache contract does not patch incomplete payloads in place.
    assert j.get("higher_macd_hist") == []
