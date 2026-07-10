# -*- coding: utf-8 -*-
"""R1-F1-2 + R1-F4-2: CL market 推断的两个盲区。

F4-2(覆盖): d45eda72 的 .US 后缀推断(cl.py __init__)是整个非 web 生产面 -5 修正的
唯一承载,却零判别性测试(专项 htf 测试三处全显式传 market=,golden 对 offset 无判别力
—— +8/-5 三 hash 逐字节相同)。推断若回归,美股 HTF 静默回退 +8 全套件恒绿。

F1-2(治愈): 修复前落盘的美股 CL pkl(market=None)经 unpickle 绕过 __init__,None
无限期存活 → cl_object_cache 续算路径(fdb.get_web_cl_data 每请求重写 pkl 刷 mtime,
15 天清理永不触发)htf 恒 +8,与新建 CL(-5)口径分裂。修复=__setstate__ 按同规则补推断。
"""
import copy
import pickle

from chanlun.core.cl import CL


# ---- F4-2: 推断钉扎(不传 market) ----

def test_infer_us_suffix():
    assert CL("QQQ.US", "30m", {}).market == "us"


def test_infer_us_suffix_case_insensitive():
    assert CL("qqq.us", "30m", {}).market == "us"


def test_infer_a_default():
    assert CL("SH.600519", "30m", {}).market == "a"


def test_explicit_market_wins_over_inference():
    assert CL("QQQ.US", "30m", {}, market="a").market == "a"


# ---- F1-2: unpickle/deepcopy 治愈遗留 market=None ----

def test_unpickle_heals_legacy_none_market_us():
    """模拟修复前落盘的美股 pkl: market=None → unpickle 后必须推断为 us。"""
    cd = CL("QQQ.US", "30m", {})
    cd.market = None                       # 强制回到旧 pkl 状态
    cd2 = pickle.loads(pickle.dumps(cd))
    assert cd2.market == "us", f"旧 pkl market=None 未被治愈: {cd2.market!r}"


def test_unpickle_heals_legacy_none_market_a():
    """A股旧 pkl: None→'a',与历史默认 +8 等价零回归。"""
    cd = CL("SH.600519", "5m", {})
    cd.market = None
    cd2 = pickle.loads(pickle.dumps(cd))
    assert cd2.market == "a"


def test_unpickle_preserves_explicit_market():
    """显式 market 的 pkl 原样保留(web 传 market 路径零影响)。"""
    cd = CL("QQQ.US", "30m", {}, market="us")
    cd2 = pickle.loads(pickle.dumps(cd))
    assert cd2.market == "us"


def test_deepcopy_pool_path_not_broken():
    """CL 池 store_cl_to_pool 走 copy.deepcopy(经 __reduce_ex__/__setstate__):
    加 __setstate__ 后 deepcopy 必须不抛且 market 语义一致。"""
    cd = CL("QQQ.US", "30m", {})
    cd.market = None
    cd2 = copy.deepcopy(cd)
    assert cd2.market == "us"
    cd3 = copy.deepcopy(CL("SZ.000001", "5m", {}))
    assert cd3.market == "a"