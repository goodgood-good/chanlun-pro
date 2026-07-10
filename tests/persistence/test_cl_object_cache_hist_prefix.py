# -*- coding: utf-8 -*-
"""R11-#2: get_web_cl_data 4 重校验覆盖窗口对任意长度序列恒封顶 ~100 根,窗口外中段历史订正/
补洞全盲,陈旧 cd 被增量续算并回写(HIGH)。

check2 采样单根 ref_idx=-max(10,min(len//4,100)),len>400 时恒为倒数第 100 根;check1/3 只看
首尾/最近 100 根;check4 只管左扩。→ 倒数第 100 根之前的中段订正四项全 PASS、need_recompute 保持
False、旧 cd 增量续算(_preprocess 只剪到 last_date 之后,从不回看)→ 该历史 bar 永久污染分型/笔/
中枢/买卖点。修复=新增全前缀 OHLC 指纹校验(按日期窗口对齐,排除末根 forming bar,与 web 版
_hist_fp 同口径)。本测试:(1)中段(idx150,远超 100 窗口)订正必触发全量重算返回订正后值;
(2)纯 append(无订正)不得误触发 check5 重算(logger 录制器检测'历史前缀'重算告警)。
"""
import datetime
import pickle

import pandas as pd

import chanlun.file_db_mixins.cl_object_cache as cache_mod
from chanlun.core import cl
from chanlun.file_db_mixins.cl_object_cache import _CLObjectCacheMixin

_CFG = {"macd_ld_use_htf": True, "recursive_zs_diversity": False}


class _Harness(_CLObjectCacheMixin):
    def __init__(self, root):
        self.cl_data_path = root

    def _config_md5(self, cl_config):
        return "testkey"

    def _atomic_write_pickle(self, path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fp:
            pickle.dump(obj, fp)

    def _try_run_cleanup(self, *a, **k):
        pass


class _RecLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def __getattr__(self, name):
        # 未定义方法(error/info/isEnabledFor/exception 等)一律 no-op,
        # 避免全局 patch LogUtil 后 process_klines 内部 logger 调用缺方法崩
        return lambda *a, **k: None


def _mk_df(n):
    rows = []
    base = datetime.datetime(2019, 1, 1)
    price = 100.0
    for i in range(n):
        rows.append({
            "date": base + datetime.timedelta(days=i),
            "open": price, "high": price + 1.0, "low": price - 1.0,
            "close": price + 0.5, "volume": 1000.0,
        })
        price += 0.3
    return pd.DataFrame(rows)


def _pkl_path(root):
    return root / "a" / "a_SH_600519_d_testkey.pkl"


def _seed_cache(root, df):
    """用 df 全量算一个 cd 落盘到 get_web_cl_data 预期路径, 模拟已有缓存。"""
    pp = _pkl_path(root)
    pp.parent.mkdir(parents=True, exist_ok=True)
    cd = cl.CL("SH.600519", "d", dict(_CFG))
    cd.process_klines(df)
    with open(pp, "wb") as fp:
        pickle.dump(cd, fp)


def test_mid_history_revision_triggers_recompute(tmp_path):
    df1 = _mk_df(700)
    orig_c = float(df1.iloc[150]["close"])
    new_c = orig_c + 0.25  # 仍在 [low, high] 内, 与原值明显不同

    _seed_cache(tmp_path, df1)

    df2 = df1.copy()
    df2.loc[150, "close"] = new_c
    extra = _mk_df(705).iloc[700:705].reset_index(drop=True)
    df2 = pd.concat([df2, extra], ignore_index=True)

    H = _Harness(tmp_path)
    result = H.get_web_cl_data("a", "SH.600519", "d", dict(_CFG), df2)

    got = float(result.get_src_klines()[150].c)
    assert abs(got - new_c) < 1e-6, f"中段订正未被感知: got={got} 期望订正后 {new_c}(陈旧值 {orig_c})"
    assert abs(got - orig_c) > 0.1, "返回的仍是陈旧未订正值"


def test_pure_append_no_false_recompute(tmp_path, monkeypatch):
    """纯 append(无中段订正)不得被 check5 误判为需重算(否则 reboot_trader 每轮全量重算=perf 退化)。"""
    df1 = _mk_df(700)
    _seed_cache(tmp_path, df1)
    df_app = _mk_df(705)  # 与 df1 前 700 根完全一致 + 5 根新 bar, 无任何订正

    rec = _RecLogger()
    monkeypatch.setattr(cache_mod.LogUtil, "get_logger", lambda *a, **k: rec)

    H = _Harness(tmp_path)
    result = H.get_web_cl_data("a", "SH.600519", "d", dict(_CFG), df_app)

    hist_warns = [w for w in rec.warnings if "历史前缀" in w]
    assert not hist_warns, f"纯 append 误触发全前缀重算: {hist_warns}"
    assert len(result.get_src_klines()) == 705