"""R9-file_db143: cl_update_date 版本 bump 时必须先 clear_all_cl_data 再写版本标记。

file_db.py:141-144 顺序颠倒: db.cache_set("__cl_update_date", ...) 先提交新版本号,
clear_all_cl_data() 后清缓存。clear 逐文件 unlink 数千文件耗时数秒, 期间进程被
kill/断电/单文件 PermissionError 被吞 → DB 已记新版本、残留旧算法 pkl 永不再清(下次
启动版本匹配直接跳过)→ 旧算法结构 cd 被新代码 process_klines 增量续算 → web 图表与
监控预警缠论静默错误, 且 _atomic_write_pickle 刷 mtime 使 15 天清理对活跃标的永不回收。
修复=反序: 先 clear 成功再写版本标记, 中断则版本保持旧值下次启动重试(恒安全)。
"""
import chanlun.persistence.file_db as fdb_mod


def test_cl_update_date_clears_before_version_set(monkeypatch):
    order = []
    # 强制 cache_get 返回旧版本, 触发 __init__ 的 bump 分支
    monkeypatch.setattr(fdb_mod.db, "cache_get", lambda k: "__OLD_VERSION_PROBE__")
    monkeypatch.setattr(fdb_mod.db, "cache_set", lambda k, v: order.append("cache_set"))
    real_cls = getattr(fdb_mod.FileCacheDB, "__wrapped__", fdb_mod.FileCacheDB)
    monkeypatch.setattr(real_cls, "clear_all_cl_data", lambda self: order.append("clear"))
    real_cls()  # 触发 __init__ 版本 bump 分支
    assert "clear" in order, "bump 分支未调用 clear_all_cl_data"
    assert "cache_set" in order, "bump 分支未写版本标记"
    assert order.index("clear") < order.index("cache_set"), (
        f"必须先 clear_all_cl_data 再 cache_set 版本标记(防清理中断致旧pkl残留), 实际顺序={order}"
    )