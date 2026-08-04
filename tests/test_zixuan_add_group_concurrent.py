"""R4-G1-1: 全局自选分组的并发创建必须保持幂等。

web app 的多个请求线程可能同时通过存在性检查并竞争同一个全局分组唯一键；输家的
IntegrityError 必须被转为 False，不能令 view 返回 500。
"""

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

    monkeypatch.setattr(zx_mod.db, "zx_add_global_group", _raise)
    # 修复前: IntegrityError 上抛→view 500;修复后: 幂等 False
    assert z.add_zx_group("新组") is False


def test_existing_name_returns_false_no_db(monkeypatch):
    z = _mk_zixuan(["已存在组"])
    # 存在性检查即返 False, 不应触达 db.zx_add_group
    called = {"n": 0}
    monkeypatch.setattr(
        zx_mod.db,
        "zx_add_global_group",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    assert z.add_zx_group("已存在组") is False
    assert called["n"] == 0


def test_reserved_name_returns_false():
    z = _mk_zixuan([])
    assert z.add_zx_group("我的关注") is False

def test_get_zx_groups_concurrent_default_group_no_raise(monkeypatch):
    """R5-H3-1(R4-G1-1 姊妹): get_zx_groups 空组时自动建默认组"我的关注"同样是无 try 的
    check-then-insert, 由 __init__ 每次构造 ZiXuan 都走。冷启/清库后两并发构造→都读 len==0→
    都 INSERT→复合主键冲突→输家 IntegrityError 逃逸 view→500。修复=catch 后重读拿到组。"""
    z = object.__new__(ZiXuan)
    z.market_type = "a"
    calls = {"get": 0}

    class _G:
        def __init__(self, name):
            self.zx_group = name

    def _get():
        calls["get"] += 1
        return (
            []
            if calls["get"] == 1
            else [_G("我的关注"), _G("我的持仓")]
        )

    def _add_raise(*a, **k):
        raise IntegrityError("INSERT INTO cl_zixuan_groups ...", None, Exception("dup"))

    monkeypatch.setattr(zx_mod.db, "zx_get_global_groups", _get)
    monkeypatch.setattr(zx_mod.db, "zx_add_global_group", _add_raise)
    # 修复前 IntegrityError 上抛; 修复后 catch→重读
    groups = z.get_zx_groups()
    assert groups == [{"name": "我的关注"}, {"name": "我的持仓"}]
    assert calls["get"] == 2
