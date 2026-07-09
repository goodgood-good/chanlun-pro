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
from chanlun.recursive_bt.sim.paper import drop_unclosed_last_bar
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
                # S4(done-gating): 丢弃末尾未收盘(在制)bar,使喂 CL 的口径与
                # 交易层 live 一致——否则评估作用在 forming bar 上,背驰面积/笔破位
                # 判据随实时行情漂移=未来函数/重画。drop_unclosed_last_bar 对日线等
                # 非分钟级、不足两根、间隔异常一律 no-op,tz 安全,绝不误删历史 bar。
                klines = drop_unclosed_last_bar(klines, f)
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
    #    去重按 (market, identity, task_name) 隔离(C7):不含 task_name 会让同市场多任务
    #    重叠标的时,后跑任务的推送与记录被先跑任务静默吞掉。
    new_signals = []
    pending_saves = []  # (sig, exists):推送成功(或纯记录任务)后再落库
    for sig in signals:
        try:
            exists = _repo.signal_record_query_by_identity(
                market, sig.identity, task_name
            )
        except Exception as e:
            _log.warning(f"[monitoring_signal_code] dedup query failed: {e}")
            exists = None
        if exists is not None and not _grade_upgraded(exists.grade, sig.grade):
            continue
        new_signals.append(sig)
        pending_saves.append((sig, exists))

    # 4. 推送(先于落库):推送任务只有推送成功才落库(见步骤5)。否则本轮不"消费"
    #    这些信号——下一轮同 identity 仍未落库会重新尝试推送,避免单次推送失败
    #    (飞书瞬时故障/限流)使该信号因永久去重而静默漏发(C6)。
    push_ok = True
    if is_send_msg and new_signals:
        msgs = [f"【{name} - {task_name}】缠论信号 {len(new_signals)} 条"]
        for sig in new_signals:
            msgs.append(
                f"[{sig.grade}] {sig.operation_level} {sig.signal_kind} "
                f"{sig.direction} 评分{sig.score} — {sig.msg}"
            )
        try:
            ok = _send(market, f"{task_name} 信号监控提醒", msgs)
            push_ok = ok is not False
            if not push_ok:
                _log.warning(
                    f"[monitoring_signal_code] send failed market={market} code={code}"
                )
        except Exception as e:
            push_ok = False
            _log.warning(f"[monitoring_signal_code] send exception: {e}")

    # 5. 落库:纯记录任务(不推送)始终落库;推送任务仅在推送成功后落库,
    #    使推送失败的信号下一轮可重试(C6)。
    if (not is_send_msg) or push_ok:
        for sig, exists in pending_saves:
            try:
                if exists is not None:
                    # S2: 分级升级路径 update 既有行,不再 add 新行——否则 cl_signal_record
                    # 随"信号数 × 升级次数"无界膨胀。去重仍按最近一条比 grade。
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

    return new_signals
