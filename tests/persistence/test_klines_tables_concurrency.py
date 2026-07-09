"""R6-#2: DB.klines_tables() 无锁裸 dict check-then-act + 动态注册同名 ORM 表类。

__cache_tables 无锁, 多线程首访同一冷表都过存在性检查、都执行 `class TableByKlines(Base)`
(向共享 Base.metadata 注册同名 Table), 第 2+ 个线程在类体末尾的 declarative 元类注册处撞
sqlalchemy InvalidRequestError('Table ... is already defined')。web 多线程 prewarm(currency
4 周期并行, 表名与 freq 无关共用一张表)可达。修复=加 _cache_tables_lock + double-check
(镜像 exchange.get_exchange 审查 B-1)。

确定性复现: race 窗口=cache 检查→class 体执行→cache 写入。stub create_all(避免真实 DDL 的
sqlite 同名索引 artifact), 并令类体内的 Index() 构造 sleep, 把多个线程同时"卡"在类体内,
保证 declarative 元类注册处必然并发撞车(无 fix 必崩); 有 fix 时锁串行化→仅首线程建表其余走
缓存→零崩。测试后从全局 Base.metadata 移除注册表, 防污染其它测试。
"""
import pathlib
import sys
import threading
import time

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))

from chanlun.persistence import db as db_mod  # noqa: E402

_DBcls = getattr(db_mod.DB, "__wrapped__", db_mod.DB)
_real_index = db_mod.Index


def _slow_index(*a, **k):
    # 令类体构造变慢, 把并发线程"卡"在 class TableByKlines(Base) 内, 确定性复现注册 race
    time.sleep(0.03)
    return _real_index(*a, **k)


def _fresh_db(monkeypatch, slow=False):
    monkeypatch.setattr(db_mod.Base.metadata, "create_all", lambda *a, **k: None)
    if slow:
        monkeypatch.setattr(db_mod, "Index", _slow_index)
    obj = _DBcls.__new__(_DBcls)
    obj.engine = None
    setattr(obj, "_DB__cache_tables", {})  # 名字改写 self.__cache_tables
    obj._cache_tables_lock = threading.Lock()
    return obj


def _drop(tbl_name):
    md = db_mod.Base.metadata
    if tbl_name in md.tables:
        md.remove(md.tables[tbl_name])


def test_klines_tables_concurrent_first_access_no_crash(monkeypatch):
    obj = _fresh_db(monkeypatch, slow=True)
    tbl_name = "us_klines_us_r6lock"
    try:
        n = 16
        barrier = threading.Barrier(n)
        errors = []
        results = []

        def worker():
            try:
                barrier.wait()
                results.append(obj.klines_tables("us", "US.R6LOCK"))
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"并发首访崩溃: {errors[:2]}"
        assert len(set(id(x) for x in results)) == 1  # 全部拿同一缓存表类
        assert len(results) == n
    finally:
        _drop(tbl_name)


def test_klines_tables_idempotent(monkeypatch):
    obj = _fresh_db(monkeypatch)
    tbl_name = "us_klines_us_r6idem"
    try:
        a = obj.klines_tables("us", "US.R6IDEM")
        b = obj.klines_tables("us", "US.R6IDEM")
        assert a is b  # 二次调用返回同一缓存类, 不重复注册
    finally:
        _drop(tbl_name)