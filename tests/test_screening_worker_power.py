import ctypes
import os
from unittest.mock import Mock

import pytest

from chanlun.screening import worker_lifetime
from chanlun.screening.worker_pool import ReusableWorkerPool
from tests.test_screening_worker_pool import echo_worker, scan, results


@pytest.mark.parametrize("handle", [None, 123])
def test_high_qos_only_disables_execution_throttling_without_priority_or_affinity_changes(
    monkeypatch, handle
):
    seen = []

    def setting(target, kind, state, size):
        values = ctypes.cast(state, ctypes.POINTER(ctypes.c_ulong * 3)).contents
        seen.append((target, kind, list(values), size))
        return True

    api = Mock()
    api.GetCurrentProcess.return_value = 456
    api.SetProcessInformation.side_effect = setting
    monkeypatch.setattr(worker_lifetime.os, "name", "nt")
    monkeypatch.setattr(worker_lifetime.ctypes, "WinDLL", lambda *a, **k: api)
    result = worker_lifetime.prefer_analysis_performance(handle)
    assert result == {
        "state": "high_qos",
        "priority_changed": False,
        "affinity_changed": False,
    }
    assert seen[0][:3] == (456 if handle is None else 123, 4, [1, 1, 0])
    api.SetPriorityClass.assert_not_called()
    api.SetProcessAffinityMask.assert_not_called()


def test_an_unavailable_power_api_does_not_abort_a_calculation(monkeypatch):
    monkeypatch.setattr(worker_lifetime.os, "name", "nt")
    monkeypatch.setattr(
        worker_lifetime.ctypes,
        "WinDLL",
        lambda *a, **k: (_ for _ in ()).throw(OSError("unsupported")),
    )
    assert worker_lifetime.prefer_analysis_performance()["state"] == "unavailable"


@pytest.mark.skipif(os.name != "nt", reason="Windows process policy")
def test_real_spawned_worker_and_coordinator_have_explicit_high_qos(tmp_path):
    pool = ReusableWorkerPool(
        "test", worker_target=echo_worker, revision_reader=lambda: "test"
    )
    try:
        rows = results(scan(pool, tmp_path / "run"))
        assert len(rows) == 1
        assert pool.job.owner_power_policy["state"] == "high_qos"
        policies = pool.snapshot()["worker_power_policies"]
        assert policies[str(rows[0]["pid"])]["state"] == "high_qos"

        class Power(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetProcessInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        value = Power(1, 0, 0)
        assert kernel.GetProcessInformation(
            pool.slots[0]["process"].sentinel,
            4,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        assert value.ControlMask & 1 and not value.StateMask & 1
    finally:
        pool.close()
