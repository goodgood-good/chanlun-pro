"""R6-#4: monitoring_code 在飞书推送前就落库(alert_record_save/marks_add), 推送失败该信号被
dedup 永久抑制 → 静默哑火。修复=延迟落库到推送成功之后(镜像 signal_monitor C6):
仅 `(not is_send_msg) or push_ok` 才写 alert_record/marks, 否则不 dedup 下轮重发。
"""
import datetime
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))

import pandas as pd  # noqa: E402

from chanlun import monitor as mon  # noqa: E402


_DT = datetime.datetime(2024, 1, 2, 10, 0, 0)


class _K:
    date = _DT


class _End:
    def ld(self):
        return {}


class _Start:
    k = _K()


class _Bi:
    type = "up"

    def __init__(self):
        self.end = _End()
        self.start = _Start()

    def is_done(self):
        return True

    def bc_exists(self, types, op="|"):
        return True  # 触发笔背驰

    def mmd_exists(self, mmds, op="|"):
        return False


class _Cd:
    def get_bis(self):
        return [_Bi()]

    def get_frequency(self):
        return "30m"

    def get_xds(self):
        return []

    def get_src_klines(self):
        return [type("SK", (), {"date": _DT})()]

    def get_code(self):
        return "X"


class _FakeEx:
    def klines(self, code, f):
        return pd.DataFrame({"date": [_DT]})


_CHECK = {
    "bi_types": ["up"], "bi_beichi": ["bi"], "bi_mmd": [],
    "xd_types": [], "xd_beichi": [], "xd_mmd": [],
}


def _wire(monkeypatch, push_ok):
    monkeypatch.setattr(mon, "get_exchange", lambda m: _FakeEx())
    monkeypatch.setattr(mon, "web_batch_get_cl_datas", lambda market, code, klines, cl_config: [_Cd()])
    monkeypatch.setattr(mon, "bi_td", lambda bi, cd: False)
    monkeypatch.setattr(mon, "kchart_to_png", lambda *a, **k: "")
    monkeypatch.setattr(mon, "send_fs_msg", lambda *a, **k: push_ok)
    saves = []
    marks = []
    monkeypatch.setattr(mon.db, "alert_record_query_by_code", lambda *a, **k: None)
    monkeypatch.setattr(mon.db, "alert_record_save", lambda *a, **k: saves.append(a))
    monkeypatch.setattr(mon.db, "marks_add_by_price", lambda *a, **k: marks.append(a))
    return saves, marks


def _run(monkeypatch, push_ok):
    saves, marks = _wire(monkeypatch, push_ok)
    mon.monitoring_code(
        "task", "us", "X", "名称", ["30m"],
        check_cl_types=_CHECK, is_send_msg=True, cl_config={},
    )
    return saves, marks


def test_push_failure_defers_save_no_dumb_mute(monkeypatch):
    # 推送失败 → 不落库(不 dedup), 下轮重发, 防哑火
    saves, marks = _run(monkeypatch, push_ok=False)
    assert saves == []
    assert marks == []


def test_push_success_saves(monkeypatch):
    # 推送成功 → 正常落库(dedup 生效)
    saves, marks = _run(monkeypatch, push_ok=True)
    assert len(saves) == 1
    assert len(marks) == 1