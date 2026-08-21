"""后台调度任务的统一中文名称。

任务编号是调度、恢复和审计协议的一部分，必须保持稳定；本模块只统一面向人的
中文名称。页面快照也会按任务编号重新映射，因此应用升级前缓存的英文名称不会
继续暴露给用户。
"""

from __future__ import annotations


JOB_DISPLAY_NAMES = {
    "holding_group_realtime_monitor": "人工关注分组跨市场实时监听",
    "qmt_app_daily_restart": "QMT 工作日启动维护（应用托管）",
    "qmt_app_runtime_monitor": "QMT 运行状态与故障恢复监控",
    "forward_capture": "统一策略前向模拟盘前快照采集（应用托管）",
    "forward_evaluate": "统一策略前向模拟盘后评估（应用托管）",
    "forward_reconcile": "统一策略前向模拟失败恢复协调",
    "forward_startup_reconcile": "统一策略前向模拟启动一致性检查",
}


def job_display_name(job_id: object, fallback: object = "--") -> str:
    """返回任务编号对应的中文名称，未知任务保留调度器提供的名称。"""

    key = str(job_id or "").strip()
    if key in JOB_DISPLAY_NAMES:
        return JOB_DISPLAY_NAMES[key]
    value = str(fallback or "").strip()
    return value or "--"


__all__ = ["JOB_DISPLAY_NAMES", "job_display_name"]
