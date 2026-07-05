"""让 tests/web 下的测试能 import web 服务模块(cl_app)与 src(chanlun)。"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))


import pytest


@pytest.fixture(autouse=True)
def _isolate_fdb_chart_cache_dir(tmp_path, monkeypatch):
    """tests/web 一律把 fdb 的 chart_cache 磁盘目录隔离到 tmp(2026-07-07 事故复盘)。

    部分测试(如 ghost 系列)经 compute_and_cache 穿透真实 D:/chanlun_pro/chart_cache:
    写测试标的 pkl,并可触发 7 天 age 的机会型清理(clear_old_chart_cache 全目录按
    mtime 删)——曾一次删除 2.7 万个已过期生产缓存文件。缓存可重建(web prewarm/按需),
    但**测试触碰生产目录绝对禁止**。
    """
    from chanlun.persistence.file_db import fdb

    d = tmp_path / "chart_cache"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fdb, "chart_cache_path", d, raising=True)
    yield
