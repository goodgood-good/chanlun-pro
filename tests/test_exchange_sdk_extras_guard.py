"""tests/test_exchange_sdk_extras_guard.py — US-006 验证 6 个 exchange 顶层 SDK
硬依赖被 try/except 包裹, ImportError 时给出 extras 提示。

策略: 用 ``monkeypatch.setitem(sys.modules, "<sdk>", None)`` 模拟"未装该 SDK"的
状态, 然后 importlib.import_module / importlib.reload, 断言:
- 抛 ImportError
- message 含 "requires extras: pip install 'chanlun-pro[<extras>]'" 关键字

这样无论本机是否装了 alpaca/polygon/futu/baostock/openctp_ctp/tqsdk, CI 都能跑通。
"""

from __future__ import annotations

import importlib
import sys

import pytest


# (exchange_module_path, hard_dep_sdk_name, expected_extras_token)
EXCHANGE_GUARDS = [
    ("chanlun.exchange.exchange_alpaca", "alpaca", "chanlun-pro[us]"),
    ("chanlun.exchange.exchange_polygon", "polygon", "chanlun-pro[us]"),
    ("chanlun.exchange.exchange_futu", "futu", "chanlun-pro[hk]"),
    ("chanlun.exchange.exchange_baostock", "baostock", "chanlun-pro[cn-extra]"),
    ("chanlun.exchange.exchange_ctp", "openctp_ctp", "chanlun-pro[futures]"),
    ("chanlun.exchange.exchange_tq", "tqsdk", "chanlun-pro[futures]"),
]


@pytest.mark.parametrize(
    "module_path,sdk_name,expected_extras",
    EXCHANGE_GUARDS,
    ids=[m for m, _, _ in EXCHANGE_GUARDS],
)
def test_exchange_module_raises_friendly_error_without_sdk(
    monkeypatch, module_path: str, sdk_name: str, expected_extras: str
):
    """模拟 SDK 未安装, exchange 模块顶层 import 必须抛 ImportError 且含 extras 提示。

    实现方法: 把 ``sys.modules[sdk_name]`` 设为 None, 这是 Python 的"屏蔽 import"
    标准技巧 — 任何 ``import <sdk_name>`` 都会立刻抛 ModuleNotFoundError 而不查
    site-packages, 完美模拟未装 extras 的环境。
    """
    # 1. 清掉已被导入的 exchange 模块缓存, 强制下面 import 走顶层 import 路径
    sys.modules.pop(module_path, None)
    # 2. 同时清掉其子模块缓存 (如 alpaca.data 等), 否则可能复用旧引用
    submodule_prefix = sdk_name + "."
    stale_keys = [k for k in list(sys.modules.keys()) if k == sdk_name or k.startswith(submodule_prefix)]
    for k in stale_keys:
        monkeypatch.delitem(sys.modules, k, raising=False)
    # 3. 屏蔽 SDK: 设为 None 让 import 抛错
    monkeypatch.setitem(sys.modules, sdk_name, None)

    with pytest.raises(ImportError) as excinfo:
        importlib.import_module(module_path)

    msg = str(excinfo.value)
    assert "requires extras" in msg, (
        f"{module_path} ImportError message 缺 'requires extras' 关键字: {msg!r}"
    )
    assert expected_extras in msg, (
        f"{module_path} ImportError message 缺 extras 提示 '{expected_extras}': {msg!r}"
    )
