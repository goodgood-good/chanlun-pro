"""H2 回归:/xuangu/task_add 蓝图必须对齐 xuangu_list.html 表单。

模板 ``xuangu_list.html`` 提交的字段是 ``src_zx_group`` + ``target_zx_group``,
``XuanguTasks.run_xuangu`` 也需要 6 个参数(含 ``target_zx_group``)。

老蓝图读的是不存在的 ``request.form["zx_group"]``(KeyError → 400),
且只给 ``run_xuangu`` 传 5 个参数(TypeError → 500)→ 端点必失败。
本测试锁定"蓝图读对字段、以 6 参调用 run_xuangu"。
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin


class _Anon(UserMixin):
    id = "test"


class _StubXuanguTasks:
    """run_xuangu 用与生产一致的 6 参签名,捕获实参供断言。"""

    def __init__(self):
        self.run_xuangu_args = None

    def xuangu_task_config_list(self):
        return {
            "xg_single_bi_1mmd": {"name": "笔的一类买卖点", "frequency_num": 1},
        }

    def run_xuangu(
        self, market, xuangu_task_name, freqs, opt_type,
        src_zx_group, target_zx_group,
    ):
        self.run_xuangu_args = (
            market, xuangu_task_name, freqs, opt_type,
            src_zx_group, target_zx_group,
        )
        return True


@pytest.fixture
def stub_tasks():
    return _StubXuanguTasks()


@pytest.fixture
def client(stub_tasks):
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.config["LOGIN_DISABLED"] = True
    flask_app.secret_key = "h2-test"

    lm = LoginManager()
    lm.init_app(flask_app)
    lm.user_loader(lambda _id: _Anon())

    from cl_app.blueprints.xuangu import xuangu_bp

    flask_app.register_blueprint(xuangu_bp)
    flask_app.extensions["xuangu_tasks"] = stub_tasks
    return flask_app.test_client()


def test_xuangu_task_add_passes_src_and_target_groups(client, stub_tasks):
    """蓝图须读 src_zx_group / target_zx_group,并以 6 参调用 run_xuangu。"""
    resp = client.post(
        "/xuangu/task_add",
        data={
            "market": "a",
            "task_name": "xg_single_bi_1mmd",
            "frequencys": "d",
            "opt_type": "long",
            "src_zx_group": "all",
            "target_zx_group": "我的关注",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert stub_tasks.run_xuangu_args == (
        "a", "xg_single_bi_1mmd", ["d"], ["long"], "all", "我的关注",
    )
