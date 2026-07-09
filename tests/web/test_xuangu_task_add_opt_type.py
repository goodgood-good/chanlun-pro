"""R6-#6: /xuangu/task_add 未校验 opt_type → 非法值(空串/拼写错)经 get_opt_types 的
direction_types[x] KeyError 被逐码吞 → xg_results 空 → 无条件 clear_zx_stocks 清空目标自选组
(静默数据丢失)。修复=task_add 早校验 opt_type∈{long,short}, 非法直接 ok:False 不进 run_xuangu。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import pytest  # noqa: E402

from cl_app import create_app  # noqa: E402


class _FakeXuanguTasks:
    def __init__(self):
        self.run_called = False

    def xuangu_task_config_list(self):
        return {"my_task": {"frequency_num": 1}}

    def run_xuangu(self, market, task_name, frequencys, opt_type, src, target):
        self.run_called = True
        return True


@pytest.fixture
def app_fake():
    app = create_app()
    app.config["LOGIN_DISABLED"] = True
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    fake = _FakeXuanguTasks()
    app.extensions["xuangu_tasks"] = fake
    return app, fake


def _post(app, opt_type):
    return app.test_client().post("/xuangu/task_add", data={
        "market": "a", "task_name": "my_task", "frequencys": "5m",
        "src_zx_group": "g1", "target_zx_group": "g2", "opt_type": opt_type,
    })


def test_task_add_rejects_empty_opt_type(app_fake):
    app, fake = app_fake
    j = _post(app, "").get_json()
    assert j["ok"] is False
    assert fake.run_called is False  # 未进 run_xuangu → 目标自选组不被清空


def test_task_add_rejects_misspelled_opt_type(app_fake):
    app, fake = app_fake
    j = _post(app, "buy").get_json()
    assert j["ok"] is False
    assert fake.run_called is False


def test_task_add_accepts_valid_opt_type(app_fake):
    app, fake = app_fake
    j = _post(app, "long,short").get_json()
    assert fake.run_called is True  # 合法值正常进 run_xuangu
    assert j["ok"] is True