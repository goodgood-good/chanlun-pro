"""M8 回归:query_cl_chart_config 的 DB 故障兜底不得污染本地缓存。

DB 读失败时返回"默认配置兜底",但若把它也写进 ``_cl_config_cache``
(TTL 300s),一次 DB 抖动就会把默认配置钉住 5 分钟 —— 即便 DB 已恢复、
用户的自选配置仍被默认值覆盖。本测试锁定:只有成功读过 DB 才缓存结果。
"""

from __future__ import annotations

import pytest

from chanlun import cl_utils


@pytest.fixture(autouse=True)
def _reset_cl_config_state():
    """每个用例前后清空配置缓存与 DB 退避状态,避免相互污染。"""
    cl_utils._cl_config_cache_invalidate()
    cl_utils._cl_config_db_backoff_until = 0.0
    yield
    cl_utils._cl_config_cache_invalidate()
    cl_utils._cl_config_db_backoff_until = 0.0


def test_db_failure_fallback_is_not_cached(monkeypatch):
    """DB 读失败 → 返回默认兜底,但不缓存;DB 恢复后应立即读到真实配置。"""

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(cl_utils.db, "cache_get", _boom)
    cfg1 = cl_utils.query_cl_chart_config("a", "M8TESTX")
    # 默认配置兜底:zs_wzgx 为 zs_wzgx_gd（默认趋势口径,原文严格档,见 cl_utils）
    assert cfg1["zs_wzgx"] == "zs_wzgx_gd"

    # 模拟退避窗口到期 + DB 恢复,返回用户自定义配置（取与默认 gd 不同的值,
    # 才能验证 cfg2 确实读到了 DB 而非沿用兜底默认）
    cl_utils._cl_config_db_backoff_until = 0.0

    def _ok(key):
        return {"zs_wzgx": "zs_wzgx_zgd"} if key.endswith("_common") else None

    monkeypatch.setattr(cl_utils.db, "cache_get", _ok)
    cfg2 = cl_utils.query_cl_chart_config("a", "M8TESTX")
    assert cfg2["zs_wzgx"] == "zs_wzgx_zgd", (
        "DB 恢复后应读到自定义配置;DB 失败兜底不应被缓存"
    )


def test_db_success_result_is_cached(monkeypatch):
    """正常路径回归:成功读 DB → 结果进缓存,二次调用不再打 DB。"""
    calls = {"n": 0}

    def _ok(key):
        calls["n"] += 1
        return {"zs_wzgx": "zs_wzgx_zgd"} if key.endswith("_common") else None

    monkeypatch.setattr(cl_utils.db, "cache_get", _ok)
    cl_utils.query_cl_chart_config("a", "M8TESTY")
    n_after_first = calls["n"]
    assert n_after_first > 0
    cl_utils.query_cl_chart_config("a", "M8TESTY")
    assert calls["n"] == n_after_first, "二次调用应命中本地缓存,不再读 DB"
