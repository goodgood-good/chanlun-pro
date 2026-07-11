# -*- coding: utf-8 -*-
"""R14-C2: AIAnalyse.analyse() 对 stock_info() 返回 None 未防御 → stock["name"](:100)
抛 TypeError → /ai/analyse 路由(ai.py:33 无 try/except)裸 500(而非该文件一贯的 JSON 错误)。

机制: :83 第一次 stock_info 结果不检查直接持有; :100(if ok 分支内)stock["name"] 对 None
取下标崩。触发=同一 exchange 实例两次独立 stock_info 调用(:83 外层 + prompt 内部 :159)
结果不一致(第一次瞬时失败 None、第二次恢复)——纯 TOCTOU, 非构造输入。与 R11 qmt /
R12 futu 的 xxx_info None 守卫同家族, 此处是漏加对称守卫的一处。修复=:83 后 None 早返回。
"""
from types import SimpleNamespace

import pandas as pd

import chanlun.tools.ai_analyse as ai_mod
from chanlun.tools.ai_analyse import AIAnalyse


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add(self, r):
        pass

    def commit(self):
        pass


def _make(stock_ret):
    obj = object.__new__(AIAnalyse)  # 绕 __init__(避 get_exchange)
    obj.market = "a"
    obj.ex = SimpleNamespace(
        stock_info=lambda code: stock_ret,
        klines=lambda code, freq: pd.DataFrame(
            {"date": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        ),
    )
    return obj


def _patch_downstream(monkeypatch, obj):
    monkeypatch.setattr(ai_mod, "query_cl_chart_config", lambda m, c: {})
    monkeypatch.setattr(ai_mod, "web_batch_get_cl_datas", lambda *a, **k: [object()])
    monkeypatch.setattr(ai_mod.db, "Session", lambda: _FakeSession())
    # prompt 打桩绕开内部第二次 stock_info + 重缠论逻辑, 使外层 :83 的 None 直达 :100
    monkeypatch.setattr(obj, "prompt", lambda cd: "dummy prompt")
    monkeypatch.setattr(
        obj, "req_llm_ai_model", lambda p: {"ok": True, "model": "m", "msg": "分析结果"}
    )


def test_analyse_stock_none_returns_graceful_not_500(monkeypatch):
    """stock_info 返回 None → analyse 优雅返回 {ok:False}, 不得抛 TypeError 致路由 500。"""
    obj = _make(None)
    _patch_downstream(monkeypatch, obj)
    res = obj.analyse("SH.600519", "5m")  # 修复前: TypeError; 修复后: {ok:False}
    assert isinstance(res, dict)
    assert res["ok"] is False


def test_analyse_stock_ok_still_works(monkeypatch):
    """回归: stock_info 正常返回时 analyse 走完整路径返回 {ok:True}, 守卫不误伤。"""
    obj = _make({"name": "贵州茅台", "code": "SH.600519"})
    _patch_downstream(monkeypatch, obj)
    res = obj.analyse("SH.600519", "5m")
    assert res["ok"] is True
    assert res["msg"] == "分析结果"

def test_analyse_llm_failure_not_faked_as_success(monkeypatch):
    """R17: req_llm_ai_model 返回 ok=False(未配 key/网络失败)→ analyse 出口须回传真实
    ok=False, 不得硬编码 ok=True 让前端(ai.js:122)误报『分析成功』并丢弃真实错因。"""
    obj = _make({"name": "贵州茅台", "code": "SH.600519"})
    _patch_downstream(monkeypatch, obj)
    # 覆盖 _patch_downstream 里恒 True 的桩, 模拟 LLM 调用失败(默认 AI_TOKEN="" 即此路径)
    monkeypatch.setattr(
        obj,
        "req_llm_ai_model",
        lambda p: {"ok": False, "model": "", "msg": "未正确配置大模型的 API key 和模型名称"},
    )
    res = obj.analyse("SH.600519", "5m")
    assert res["ok"] is False, "LLM 失败不得被吞成 ok=True"
    assert res["msg"] == "未正确配置大模型的 API key 和模型名称", "真实错因须回传"