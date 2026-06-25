# -*- coding: utf-8 -*-
"""中枢-free 缠论买卖信号监控 —— 单标的评估编排。

``monitoring_signal_code`` 是自动监控的单标的入口：拉级别梯队 K 线 → 评估器
出信号 → 按 ``identity`` 去重 → 飞书推送。与项目原有 ``chanlun.monitor`` 无关，
互不影响。
"""
from __future__ import annotations

from chanlun import fun
from chanlun.cl_utils import web_batch_get_cl_datas
from chanlun.exchange import Market, get_exchange
from chanlun.signal_monitor import repository
from chanlun.signal_monitor.evaluator import ClSignalEvaluator, EvaluatorConfig
from chanlun.utils import send_fs_msg

_SIGNAL_GRADE_RANK = {"C": 0, "B": 1, "A": 2}


def _grade_upgraded(old_grade: str, new_grade: str) -> bool:
    """新分级是否高于旧分级（决定是否对已报信号重新提醒）。"""
    return _SIGNAL_GRADE_RANK.get(new_grade, 0) > _SIGNAL_GRADE_RANK.get(old_grade, 0)


def monitoring_signal_code(
    task_name: str,
    market: str,
    code: str,
    name: str,
    config: EvaluatorConfig,
    cl_config: dict = None,
    cds_by_level: dict = None,
    is_send_msg: bool = False,
    repo=None,
    send_fn=None,
):
    """对单个标的评估中枢-free 缠论买卖信号，按 identity 去重后推送。

    :param config: ``EvaluatorConfig`` 评估配置（操作级别 + 级别梯队 + 信号类型 + 最低分级）
    :param cds_by_level: 可注入的级别梯队缠论数据 ``{level: ICL}``；为 None 时按
                         ``config.level_ladder`` 自动拉 K 线并计算
    :param repo: 持久化层（默认本包 ``repository`` 模块），便于测试注入
    :param send_fn: 推送函数（默认 ``send_fs_msg``），便于测试注入
    :return: 本轮新增（需提醒）的 ``ClSignal`` 列表
    """
    _repo = repo if repo is not None else repository
    _send = send_fn if send_fn is not None else send_fs_msg
    _log = fun.get_logger()

    # 1. 准备级别梯队缠论数据
    if cds_by_level is None:
        cds_by_level = {}
        ex = get_exchange(Market(market))
        for f in config.level_ladder:
            try:
                klines = ex.klines(code, f)
                cds = web_batch_get_cl_datas(market, code, {f: klines}, cl_config)
                if cds:
                    cds_by_level[f] = cds[0]
            except Exception as e:
                _log.warning(
                    f"[monitoring_signal_code] fetch/compute failed "
                    f"market={market} code={code} freq={f} err={e}"
                )

    if config.operation_level not in cds_by_level:
        return []

    # 2. 评估
    try:
        signals = ClSignalEvaluator(market, code, name).evaluate(cds_by_level, config)
    except Exception as e:
        _log.warning(
            f"[monitoring_signal_code] evaluate failed "
            f"market={market} code={code} err={e}"
        )
        return []

    # 3. 去重：identity 无记录、或分级升级，才算需要提醒的新信号
    new_signals = []
    for sig in signals:
        try:
            exists = _repo.signal_record_query_by_identity(market, sig.identity)
        except Exception as e:
            _log.warning(f"[monitoring_signal_code] dedup query failed: {e}")
            exists = None
        if exists is not None and not _grade_upgraded(exists.grade, sig.grade):
            continue
        new_signals.append(sig)
        try:
            if exists is not None:
                # S2: 分级升级路径 update 既有行,不再 add 新行——否则 cl_signal_record 随
                # "信号数 × 升级次数"无界膨胀。去重仍按最近一条比 grade,推送行为不变。
                _repo.signal_record_update(
                    exists.id, grade=sig.grade, score=sig.score,
                    alert_msg=sig.msg, signal_dt=sig.k_date,
                )
            else:
                _repo.signal_record_save(
                    market=market, task_name=task_name, stock_code=code,
                    stock_name=name, operation_level=sig.operation_level,
                    signal_kind=sig.signal_kind, direction=sig.direction,
                    identity=sig.identity, grade=sig.grade, score=sig.score,
                    alert_msg=sig.msg, signal_dt=sig.k_date,
                )
        except Exception as e:
            _log.warning(f"[monitoring_signal_code] record save failed: {e}")

    # 4. 推送
    if is_send_msg and new_signals:
        msgs = [f"【{name} - {task_name}】缠论信号 {len(new_signals)} 条"]
        for sig in new_signals:
            msgs.append(
                f"[{sig.grade}] {sig.operation_level} {sig.signal_kind} "
                f"{sig.direction} 评分{sig.score} — {sig.msg}"
            )
        try:
            ok = _send(market, f"{task_name} 信号监控提醒", msgs)
            if ok is False:
                _log.warning(
                    f"[monitoring_signal_code] send failed market={market} code={code}"
                )
        except Exception as e:
            _log.warning(f"[monitoring_signal_code] send exception: {e}")

    return new_signals
