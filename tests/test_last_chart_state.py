"""last_chart_state 单元测试 (B5 part 1)。"""
import json
import os
import time

import pytest


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """重定向 chanlun.config.get_data_path 到 tmp_path；重置模块全局防抖状态。"""
    import chanlun.config as cfg
    monkeypatch.setattr(cfg, "get_data_path", lambda: tmp_path)
    from cl_app.services import last_chart_state as mod
    mod._last_record = None
    return tmp_path


def test_round_trip(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        record_user_request, load_last_state
    )
    record_user_request("a", "SH.000001", "1m")
    state = load_last_state()
    assert state is not None
    assert state["market"] == "a"
    assert state["code"] == "SH.000001"
    assert state["frequency"] == "1m"


def test_load_when_no_file(isolated_data_dir):
    from cl_app.services.last_chart_state import load_last_state
    assert load_last_state() is None


def test_debounce_5_seconds(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        record_user_request, load_last_state
    )
    record_user_request("a", "AA", "1m")
    state1 = load_last_state()
    ts1 = state1["updated_at"]
    time.sleep(0.05)
    record_user_request("a", "AA", "1m")
    state2 = load_last_state()
    assert state2["updated_at"] == ts1
    record_user_request("hk", "BB", "5m")
    state3 = load_last_state()
    assert state3["code"] == "BB"


def test_version_mismatch_returns_none(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        load_last_state, _state_path
    )
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 999, "market": "a", "code": "x", "frequency": "1m"}, f)
    assert load_last_state() is None


def test_missing_field_returns_none(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        load_last_state, _state_path
    )
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "market": "a"}, f)
    assert load_last_state() is None


def test_corrupt_file_returns_none(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        load_last_state, _state_path
    )
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("not-json{{{")
    assert load_last_state() is None
