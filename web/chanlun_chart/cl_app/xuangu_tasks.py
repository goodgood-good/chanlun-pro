import datetime
import threading
import traceback
from typing import Dict, List

from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.schedulers.background import BackgroundScheduler
from chanlun import zixuan
from chanlun import fun
from chanlun import utils
from chanlun.exchange import Market, get_exchange
from chanlun.xuangu import xuangu
from chanlun.trader.online_market_datas import OnlineMarketDatas
from tqdm.auto import tqdm
from chanlun.cl_utils import query_cl_chart_config

log = fun.get_logger()

# 选股运行配置
xuangu_task_configs: Dict[str, Dict[str, object]] = {
    "xg_single_bi_1mmd": {
        "name": "笔的一类买卖点",
        "task_fun": xuangu.xg_single_bi_1mmd,
        "task_memo": "笔的一类买卖点，并且所在的中枢笔数量小于9",
        "frequency_num": 1,
        "frequency_memo": "单周期",
    },
    "xg_single_bi_2mmd": {
        "name": "笔的二类买卖点",
        "task_fun": xuangu.xg_single_bi_2mmd,
        "task_memo": "笔的二类买卖点，并且所在的中枢笔数量小于9",
        "frequency_num": 1,
        "frequency_memo": "单周期",
    },
    "xg_single_bi_3mmd": {
        "name": "笔的三类买卖点",
        "task_fun": xuangu.xg_single_bi_3mmd,
        "task_memo": "笔的三类买卖点，并且所在的中枢笔数量小于9",
        "frequency_num": 1,
        "frequency_memo": "单周期",
    },
    "xg_single_find_3buy_by_1buy": {
        "name": "一类买卖点后的三类买卖点",
        "task_fun": xuangu.xg_single_find_3buy_by_1buy,
        "task_memo": "找三类买卖点，前提是前面中枢内有一类买卖点（不同的中枢配置，筛选的条件会有差异）",
        "frequency_num": 1,
        "frequency_memo": "单周期",
    },
    "xg_single_find_3buy_by_zhuanzhe": {
        "name": "趋势下跌后的三类买卖点",
        "task_fun": xuangu.xg_single_find_3buy_by_zhuanzhe,
        "task_memo": "找三类买卖点，之前段内要有是一个上涨/下跌趋势，后续趋势结束，出现转折中枢的三买（缠论的笔中枢配置要是段内中枢）",
        "frequency_num": 1,
        "frequency_memo": "单周期",
    },
    "xg_single_xd_and_bi_mmd": {
        "name": "线段和笔都有出现买点",
        "task_fun": xuangu.xg_single_xd_and_bi_mmd,
        "task_memo": "线段和笔都有出现买点",
        "frequency_num": 1,
        "frequency_memo": "单周期",
    },
    "xg_multiple_xd_bi_mmd": {
        "name": "高级别线段买点或背驰，并且次级别笔买点或背驰",
        "task_fun": xuangu.xg_multiple_xd_bi_mmd,
        "task_memo": "高级别线段买点或背驰，并且次级别笔买点或背驰",
        "frequency_num": 2,
        "frequency_memo": "两个周期",
    },
    "xg_multiple_low_level_12mmd": {
        "name": "高级别出现背驰或者买卖点，并且低级别中出现一二类买点",
        "task_fun": xuangu.xg_multiple_low_level_12mmd,
        "task_memo": "高级别出现背驰或者买卖点，并且在低级别中，其中有任意一个低级别有出现过1/2类买点",
        "frequency_num": 3,
        "frequency_memo": "三个周期",
    },
    "xg_single_ma_250": {
        "name": "均线250选股",
        "task_fun": xuangu.xg_single_ma_250,
        "task_memo": "最新价格在均线 250 之上 或者 之下",
        "frequency_num": 1,
        "frequency_memo": "一个周期",
    },
}

# ``None`` 是选股函数的正常“未命中”结果，不能再同时表示代码评估异常；否则当
# 所有代码都异常时，任务会把“全失败”误当成“合法 0 命中”并清空目标自选组。
_CODE_EVALUATION_FAILED = object()


def process_xuangu_by_code(args):
    try:
        code, market, frequencys, task_name, opt_types = args
        ex = get_exchange(Market(market))
        cl_config = query_cl_chart_config(market, "----")
        task_fun = xuangu_task_configs[task_name]["task_fun"]
        # 选股场景下同一只票多周期会复用底层缠论数据，开缓存能显著减少重复拉行情/重复计算。
        # OnlineMarketDatas 的缓存生命周期与对象同生共死，每个 code 一个新实例，
        # 不会跨 code 串数据，因此开 use_cache=True 是安全的。
        mk_datas = OnlineMarketDatas(market, frequencys, ex, cl_config, use_cache=True)
        xg_res = task_fun(code, mk_datas, opt_types)
        return xg_res
    except Exception as e:
        # 仅打印 e 时排查时基本无法定位（看不到栈），尤其是多进程/线程混跑场景；
        # 同时输出 traceback 才能在线上日志里看到具体出错文件/行号。
        tqdm.write(
            f"{market} {code} {frequencys} 执行选股任务 {task_name} 失败：{e}\n"
            f"{traceback.format_exc()}"
        )
        log.warning(
            f"process_xuangu_by_code failed market={market} code={code} "
            f"freqs={frequencys} task={task_name} err={e}",
        )
        return _CODE_EVALUATION_FAILED


def process_xuangu_task(
    market: str,
    task_name: str,
    freqs: List[str],
    opt_types: List[str],
    src_zx_group: str,
    target_zx_group: str,
):
    """
    执行选股的任务
    """
    log.info(f"{market} 开始执行选股任务 {task_name}")
    try:
        zx = zixuan.ZiXuan(market)
        ex = get_exchange(Market(market))
        run_codes = []
        if src_zx_group == "all":
            # 获取交易所下的股票代码。cq singleton 的 default_market 会被后初始化
            # 市场覆盖, 必须显式传 market(统一入口 _safe_all_stocks, 历史实测复现)。
            from .services.stock_list import _safe_all_stocks

            run_codes = [_s["code"] for _s in _safe_all_stocks(ex, market)]
            if market == "a":
                run_codes = [
                    _c
                    for _c in run_codes
                    if _c[0:5] in ["SZ.00", "SZ.30", "SH.60", "SH.68"]
                ]
            if market == "futures":
                run_codes = [_c for _c in run_codes if _c[-2:] == "L8"]
        else:
            if src_zx_group not in zx.zx_names:
                log.warning(
                    f"process_xuangu_task unknown source group market={market} "
                    f"group={src_zx_group}"
                )
                return False
            run_codes = zx.zx_stocks(src_zx_group)
            run_codes = [_s["code"] for _s in run_codes]

        tqdm.write(f"{market} {task_name} 选股任务开始，选股代码数量 {len(run_codes)}")
        # 先把命中结果写到中间 list，全部跑完后再 clear + 一次性写入，
        # 避免中途崩溃导致目标自选组被清空。
        xg_results = []
        failed_codes = 0

        for _code in tqdm(run_codes, desc="选股进度"):
            _xg_res = process_xuangu_by_code(
                (_code, market, freqs, task_name, opt_types)
            )
            if _xg_res is _CODE_EVALUATION_FAILED:
                failed_codes += 1
                continue
            if _xg_res is not None:
                tqdm.write(
                    f"{market} {task_name} 选择 {_xg_res['code']} : {_xg_res['msg']}"
                )
                xg_results.append(_xg_res)

        if failed_codes > 0:
            log.error(
                f"process_xuangu_task code evaluation failed market={market} "
                f"task={task_name} failed={failed_codes} total={len(run_codes)}; "
                "keep target group unchanged"
            )
            return False

        # 所有代码均正常完成后才以单事务发布完整快照；部分失败或写入异常时旧结果保持不变。
        if zx.replace_zx_stocks(target_zx_group, xg_results) is False:
            log.error(
                f"process_xuangu_task target group missing market={market} "
                f"task={task_name} group={target_zx_group}"
            )
            return False

        xg_stocks = zx.zx_stocks(target_zx_group)
        utils.send_fs_msg(
            market,
            f"选股任务：{xuangu_task_configs[task_name]['name']}",
            f"{xuangu_task_configs[task_name]['name']} 选股完成，选出 {len(xg_stocks)} 只代码",
        )

    except Exception as e:
        tqdm.write(f"{market} {task_name} 异常 ：{e}")
        log.exception(
            f"process_xuangu_task failed market={market} task={task_name}"
        )
        return False
    return True


def process_xuangu_job(*args) -> bool:
    """APScheduler 入口：业务失败必须抛出，才能产生 EVENT_JOB_ERROR。"""
    if process_xuangu_task(*args) is False:
        raise RuntimeError("xuangu task failed")
    return True


class XuanguTasks(object):
    def __init__(self, scheduler: BackgroundScheduler):
        self.scheduler = scheduler
        if scheduler is not None:
            if not hasattr(scheduler, "my_task_list"):
                scheduler.my_task_list = {}
            if not hasattr(scheduler, "my_task_lock"):
                scheduler.my_task_lock = threading.RLock()

    def xuangu_task_config_list(self) -> dict:
        return xuangu_task_configs

    def run_xuangu(
        self,
        market: str,
        xuangu_task_name: str,
        freqs: List[str],
        opt_type: List[str],
        src_zx_group: str,
        target_zx_group: str,
    ):
        """
        执行选个股
        """
        if xuangu_task_name not in xuangu_task_configs.keys():
            return False
        if self.scheduler is None or not bool(getattr(self.scheduler, "running", False)):
            raise RuntimeError("scheduler is not running")

        # task_id 必须包含 freqs 与目标自选组，否则同一个选股任务在不同周期 / 不同
        # 目标分组下并发跑会被误判成"上一次任务还没结束"而被拒绝。
        freqs_part = "-".join(freqs) if freqs else "nofreq"
        task_id = f"{market}_{xuangu_task_name}_{freqs_part}_{target_zx_group}"
        # 仅当任务确实"活跃/待运行"时才拒绝重发;原判据 `state != "已完成"` 会把终态
        # "执行异常"(job 抛异常)/"未执行"(missed)/"删除作业"(date job 跑完自动移除,该事件可能
        # 在 JOB_EXECUTED 之后最后落定)也误判成"未结束"→ 任务永久锁死直到重启 web(审查 F2)。
        # 改为只拒绝真正占用中的状态(运行中/等待运行/已添加),终态/异常/已删一律放行重跑。
        _ACTIVE_XG_STATES = {"运行中", "等待运行", "已添加"}
        task_name = f"{market}:{xuangu_task_configs[xuangu_task_name]['name']} {freqs} -> 【{target_zx_group}】"
        task_lock = self.scheduler.my_task_lock

        with task_lock:
            current = self.scheduler.my_task_list.get(task_id)
            if current and current.get("state") in _ACTIVE_XG_STATES:
                return False

            previous = current
            self.scheduler.my_task_list[task_id] = {
                "id": task_id,
                "name": task_name,
                "update_dt": fun.datetime_to_str(datetime.datetime.now()),
                "next_run_dt": "--",
                "state": "等待运行",
            }
            try:
                self.scheduler.add_job(
                    func=process_xuangu_job,
                    args=(
                        market,
                        xuangu_task_name,
                        freqs,
                        opt_type,
                        src_zx_group,
                        target_zx_group,
                    ),
                    trigger="date",
                    next_run_time=datetime.datetime.now(),
                    id=task_id,
                    name=task_name,
                )
            except ConflictingIdError:
                if previous is None:
                    self.scheduler.my_task_list.pop(task_id, None)
                else:
                    self.scheduler.my_task_list[task_id] = previous
                return False
            except Exception:
                if previous is None:
                    self.scheduler.my_task_list.pop(task_id, None)
                else:
                    self.scheduler.my_task_list[task_id] = previous
                raise
        return True

if __name__ == "__main__":
    xt = XuanguTasks(None)
    print(
        process_xuangu_task(
            "a", "xg_single_xd_and_bi_mmd", ["d"], ["long"], "all", "测试"
        )
    )
