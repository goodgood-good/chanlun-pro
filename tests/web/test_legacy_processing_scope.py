"""Legacy Web processing entry points stay inside the small validation scope."""

from __future__ import annotations

from pathlib import Path

import pytest

from cl_app import create_app
from cl_app.blueprints import symbols as symbols_blueprint
from cl_app.blueprints import xuangu as xuangu_blueprint


class _FakeExchange:
    @staticmethod
    def support_frequencys():
        return {"5m": "5分钟"}


class _FakeXuanguTasks:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    @staticmethod
    def xuangu_task_config_list():
        return {"my_task": {"frequency_num": 1}}

    def run_xuangu(self, *args):
        self.calls.append(args)
        return True


class _FakePrewarmManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def start(self, market: str, codes: list[dict[str, str]]) -> dict[str, object]:
        self.calls.append((market, codes))
        return {
            "ok": True,
            "msg": "started",
            "task": {"market": market, "total": len(codes), "status": "running"},
        }


@pytest.fixture
def scoped_client(monkeypatch):
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    xuangu_tasks = _FakeXuanguTasks()
    prewarm_manager = _FakePrewarmManager()
    app.extensions["xuangu_tasks"] = xuangu_tasks
    monkeypatch.setattr(xuangu_blueprint, "get_exchange", lambda _market: _FakeExchange())
    monkeypatch.setattr(symbols_blueprint, "_prewarm_manager", prewarm_manager)

    # A bounded prewarm request must not load the market catalog just to map names.
    monkeypatch.setattr(
        symbols_blueprint,
        "get_cached_processed_stocks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bounded prewarm expanded the market catalog")
        ),
    )
    return app.test_client(), xuangu_tasks, prewarm_manager


def _codes(count: int) -> str:
    return ",".join(f"SZ.{index:06d}" for index in range(1, count + 1))


def _xuangu_payload(**overrides) -> dict[str, str]:
    payload = {
        "market": "a",
        "task_name": "my_task",
        "frequencys": "5m",
        "target_zx_group": "result",
        "opt_type": "long",
        "codes": "SZ.000001,SH.600000",
    }
    payload.update(overrides)
    return payload


def test_xuangu_rejects_all_and_missing_explicit_codes_before_scheduling(scoped_client):
    client, tasks, _manager = scoped_client

    all_response = client.post(
        "/xuangu/task_add",
        data=_xuangu_payload(src_zx_group="all"),
    )
    missing_response = client.post(
        "/xuangu/task_add",
        data=_xuangu_payload(codes="", src_zx_group="watchlist"),
    )

    assert all_response.status_code == 400
    assert all_response.get_json()["code"] == "full_market_source_forbidden"
    assert missing_response.status_code == 400
    assert missing_response.get_json()["code"] == "explicit_codes_required"
    assert tasks.calls == []


def test_xuangu_defaults_to_twelve_and_has_a_twenty_symbol_hard_cap(scoped_client):
    client, tasks, _manager = scoped_client

    default_overflow = client.post(
        "/xuangu/task_add",
        data=_xuangu_payload(codes=_codes(13)),
    )
    admitted = client.post(
        "/xuangu/task_add",
        data=_xuangu_payload(codes=_codes(20), scope_limit="20"),
    )
    hard_overflow = client.post(
        "/xuangu/task_add",
        data=_xuangu_payload(codes=_codes(21), scope_limit="21"),
    )

    assert default_overflow.status_code == 403
    assert admitted.status_code == 200
    assert admitted.get_json()["ok"] is True
    assert tasks.calls[0][4] == _codes(20).split(",")
    assert tasks.calls[0][6] == 20
    assert hard_overflow.status_code == 403
    assert len(tasks.calls) == 1


def test_prewarm_requires_confirmation_and_explicit_codes(scoped_client):
    client, _tasks, manager = scoped_client

    no_confirmation = client.post(
        "/symbols/prewarm",
        data={"market": "a", "codes": "SZ.000001"},
    )
    no_codes = client.post(
        "/symbols/prewarm",
        data={"market": "a", "confirm_explicit_scope": "1"},
    )
    all_sentinel = client.post(
        "/symbols/prewarm",
        data={"market": "a", "codes": "all", "confirm_explicit_scope": "1"},
    )

    assert no_confirmation.status_code == 400
    assert no_confirmation.get_json()["code"] == "explicit_scope_confirmation_required"
    assert no_codes.status_code == 400
    assert no_codes.get_json()["code"] == "explicit_codes_required"
    assert all_sentinel.status_code == 400
    assert manager.calls == []


def test_prewarm_is_bounded_to_explicit_twenty_symbol_cohort(scoped_client):
    client, _tasks, manager = scoped_client
    base = {"market": "a", "confirm_explicit_scope": "1"}

    default_overflow = client.post(
        "/symbols/prewarm",
        data={**base, "codes": _codes(13)},
    )
    admitted = client.post(
        "/symbols/prewarm",
        data={**base, "codes": _codes(20), "scope_limit": "20"},
    )
    hard_overflow = client.post(
        "/symbols/prewarm",
        data={**base, "codes": _codes(21), "scope_limit": "21"},
    )

    assert default_overflow.status_code == 403
    assert admitted.status_code == 200
    assert [item["code"] for item in manager.calls[0][1]] == _codes(20).split(",")
    assert hard_overflow.status_code == 403
    assert len(manager.calls) == 1


def test_prewarm_manager_defensively_rejects_more_than_twenty_without_starting():
    manager = symbols_blueprint.PrewarmManager()
    codes = [{"code": code, "name": code} for code in _codes(21).split(",")]

    result = manager.start("a", codes)

    assert result["ok"] is False
    assert result["code"] == "scope_exceeded"
    assert result["task"] is None
    assert manager._worker_thread is None


def test_legacy_pages_expose_only_explicit_small_scope_controls():
    templates = Path("web/chanlun_chart/cl_app/templates")
    xuangu = (templates / "xuangu_list.html").read_text(encoding="utf-8")
    index = (templates / "index.html").read_text(encoding="utf-8")

    assert 'name="src_zx_group"' not in xuangu
    assert 'value="all"' not in xuangu
    assert 'name="codes"' in xuangu
    assert 'name="scope_limit"' in xuangu
    assert 'value="12" selected' in xuangu
    assert 'value="20"' in xuangu

    assert 'id="symbols_prewarm_codes"' in index
    assert 'id="symbols_prewarm_scope_limit"' in index
    assert "confirm_explicit_scope: '1'" in index
    assert "codes: explicitCodes.join(',')" in index
