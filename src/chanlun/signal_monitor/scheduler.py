# -*- coding: utf-8 -*-
"""信号监控的 APScheduler 调度。

``register_signal_jobs(scheduler)`` 由 web 的 ``create_app`` 调用一次即可 ——
这是 ``signal_monitor`` 包对原有代码的**唯一接线点**（且在 app 组装入口）。
job id 加 ``signal_`` 前缀，与 legacy ``cl_alert_task`` 的 job 隔离。
"""
from __future__ import annotations

from typing import List

from apscheduler.triggers.interval import IntervalTrigger
from tqdm.auto import tqdm

from chanlun import fun
from chanlun.cl_utils import query_cl_chart_config
from chanlun.exchange import Market, get_exchange, market_now_trading
from chanlun.signal_monitor import monitor as sm_monitor
from chanlun.signal_monitor import repository
from chanlun.signal_monitor.evaluator import EvaluatorConfig
from chanlun.zixuan import ZiXuan

_log = fun.get_logger()


def signal_alert_run(signal_task_id) -> bool:
    """执行一个信号监控任务：遍历自选组逐只调 ``monitoring_signal_code``。"""
    configs = repository.signal_task_query(id=signal_task_id)
    if not configs:
        return True
    task = configs[0]
    if task.is_run != 1:
        # 任务已停用(SQL 置 is_run=0)但 job 仍注册在 APScheduler(仅 create_app 时按
        # 当时 is_run 过滤注册, scheduler.py 内),不加此判会持续执行到 web 重启才停(C8)。
        return True

    ex = get_exchange(Market(task.market))
    if market_now_trading(ex, task.market) is False:
        return True

    ladder = [s.strip() for s in (task.level_ladder or "").split(",") if s.strip()]
    signal_kinds = tuple(
        s.strip() for s in (task.signal_kinds or "").split(",") if s.strip()
    )
    try:
        eval_kwargs = dict(
            operation_level=task.operation_level,
            level_ladder=ladder,
            min_grade=task.min_grade or "C",
        )
        if signal_kinds:
            eval_kwargs["signal_kinds"] = signal_kinds
        eval_config = EvaluatorConfig(**eval_kwargs)
    except Exception as e:
        _log.error(f"信号监控任务 {signal_task_id} 配置非法: {e}")
        return True

    zx = ZiXuan(task.market)
    stocks = zx.zx_stocks(task.zx_group)
    _log.info(
        f"执行 {task.task_name} 信号监控，获取 {task.zx_group} 自选组中 {len(stocks)} 数量股票"
    )
    for s in tqdm(stocks):
        try:
            cl_config = query_cl_chart_config(task.market, s["code"])
            sm_monitor.monitoring_signal_code(
                task.task_name, task.market, s["code"], s["name"],
                eval_config, cl_config=cl_config,
                is_send_msg=bool(task.is_send_msg),
            )
        except Exception as e:
            _log.error(f'signal run {s["code"]} exception {e}')
    return True


def register_signal_jobs(scheduler) -> List[str]:
    """把所有 ``is_run=1`` 的信号监控任务注册到 APScheduler。

    :return: 注册成功的 job id 列表
    """
    job_ids: List[str] = []
    try:
        tasks = repository.signal_task_query()
    except Exception as e:
        _log.error(f"register_signal_jobs 读取任务失败: {e}")
        return job_ids

    for t in tasks:
        if t.is_run != 1:
            continue
        interval_minutes = max(int(t.interval_minutes or 1), 1)
        job = scheduler.add_job(
            func=signal_alert_run,
            trigger=IntervalTrigger(minutes=interval_minutes),
            args=(t.id,),
            id=f"signal_{t.id}",
            name=f"信号监控-{t.task_name}",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        job_ids.append(job.id)
    return job_ids
