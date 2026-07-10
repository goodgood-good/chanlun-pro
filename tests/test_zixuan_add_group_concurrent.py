"""R4-G1-1: opt_zixuan_group 加组走 ZiXuan.add_zx_group 的 check-then-insert(先查存在性再
db.zx_add_group 插入)无原子性也无 try;web app 单进程 32 线程池(app.py http_workers 默认32)
下两并发 POST 同名组→都过存在性检查→都 INSERT→cl_zixuan_groups 复合主键(market,zx_group)
冲突→输家 db.zx_add_group rollback+raise IntegrityError→view(zixuan.py:79)无捕获→Flask 500。
修复=add_zx_group catch IntegrityError 幂等返 False(组已由并发赢家建成)。"""

from types import SimpleNamespace  # noqa: F401

from sqlalchemy.exc import IntegrityError

from chanlun import zixuan as zx_mod
from chanlun.zixuan import ZiXuan


def _mk_zixuan(names):
    # 绕过 __init__(其 get_zx_groups 需真实 db);直接注入状态
    z = object.__new__(ZiXuan)
    z.market_type = "a"
    z.zixuan_list = [{"name": n} for n in names]
    z.zx_names = list(names)
    return z


def test_concurrent_duplicate_returns_false_not_raise(monkeypatch):
    z = _mk_zixuan([])  # 组不存在→过存在性检查进入插入分支

    def _raise(*a, **k):
        raise IntegrityError("INSERT INTO cl_zixuan_groups ...", None, Exception("UNIQUE"))

    monkeypatch.setattr(zx_mod.db, "zx_add_group", _raise)
    # 修复前: IntegrityError 上抛→view 500;修复后: 幂等 False
    assert z.add_zx_group("新组") is False


def test_existing_name_returns_false_no_db(monkeypatch):
    z = _mk_zixuan(["已存在组"])
    # 存在性检查即返 False, 不应触达 db.zx_add_group
    called = {"n": 0}
    monkeypatch.setattr(zx_mod.db, "zx_add_group", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    assert z.add_zx_group("已存在组") is False
    assert called["n"] == 0


def test_reserved_name_returns_false():
    z = _mk_zixuan([])
    assert z.add_zx_group("我的关注") is False