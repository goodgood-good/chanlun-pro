"""The recursive 30m background pass is suspended with movement partitioning."""

from __future__ import annotations


def ensure(_screening, _manual, *, monitor_runs=None):
    """Do not launch the former recursive trend classification worker."""

    return None


def snapshot(_screening, _run_id, _revision):
    return {"status": "disabled", "completed": 0, "total": 0, "rows": {}}


def status_version(_screening, _run_id):
    return "disabled"
