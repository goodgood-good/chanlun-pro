"""选股任务只接受明确支持的 long/short 方向。"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import pytest  # noqa: E402

from cl_app import create_app  # noqa: E402
from cl_app.blueprints import xuangu as xuangu_blueprint  # noqa: E402


class _FakeExchange:
    @staticmethod
    def support_frequencys():
        return {"5m": {}, "30m": {}}


class _FakeXuanguTasks:
    def __init__(self):
        self.run_called = False

    def xuangu_task_config_list(self):
        return {
            "my_task": {"frequency_num": 1},
            "two_freq_task": {"frequency_num": 2},
        }

    def run_xuangu(self, market, task_name, frequencys, opt_type, src, target):
        self.run_called = True
        return True


@pytest.fixture
def app_fake(monkeypatch):
    app = create_app(test_config={
        "TESTING": True,
        "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
        "WTF_CSRF_ENABLED": False,
    })
    fake = _FakeXuanguTasks()
    app.extensions["xuangu_tasks"] = fake
    monkeypatch.setattr(xuangu_blueprint, "get_exchange", lambda _market: _FakeExchange())
    return app, fake


def _post(app, opt_type, frequencys="5m", task_name="my_task"):
    return app.test_client().post("/xuangu/task_add", data={
        "market": "a", "task_name": task_name, "frequencys": frequencys,
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


@pytest.mark.parametrize(
    ("frequencys", "task_name"),
    [("", "my_task"), ("5m,", "two_freq_task")],
)
def test_task_add_rejects_empty_frequency_segments(app_fake, frequencys, task_name):
    app, fake = app_fake
    j = _post(app, "long", frequencys=frequencys, task_name=task_name).get_json()
    assert j["ok"] is False
    assert fake.run_called is False


def test_task_add_rejects_unsupported_frequency(app_fake):
    app, fake = app_fake
    j = _post(app, "long", frequencys="bad").get_json()
    assert j["ok"] is False
    assert fake.run_called is False


def test_task_add_rejects_duplicate_opt_type(app_fake):
    app, fake = app_fake
    j = _post(app, "long,long").get_json()
    assert j["ok"] is False
    assert fake.run_called is False


@pytest.mark.parametrize("frequencys", ("5m,5m", "5m,30m"))
def test_task_add_rejects_duplicate_or_low_to_high_frequency_order(
    app_fake,
    frequencys,
):
    app, fake = app_fake

    response = _post(
        app,
        "long",
        frequencys=frequencys,
        task_name="two_freq_task",
    )

    assert response.get_json()["ok"] is False
    assert fake.run_called is False


def test_task_add_reports_stopped_scheduler(app_fake, monkeypatch):
    app, fake = app_fake
    monkeypatch.setattr(
        fake,
        "run_xuangu",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("scheduler is not running")
        ),
    )

    response = _post(app, "long")

    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "msg": "任务调度器未运行，请使用正式启动入口。",
    }
