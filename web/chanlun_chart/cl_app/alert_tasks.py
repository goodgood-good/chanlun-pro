import json
import threading
from typing import Dict, List

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from tqdm.auto import tqdm

from chanlun import fun
from chanlun.cl_utils import query_cl_chart_config
from chanlun.persistence.db import TableByAlertTask, db
from chanlun.exchange import Market, get_exchange, market_now_trading
from chanlun.zixuan import ZiXuan


class AlertTasks(object):
    def __init__(self, scheduler: BackgroundScheduler):
        """
        异步执行后台定时任务
        """
        self.scheduler: BackgroundScheduler = scheduler
        self.task_ids = []
        self._run_lock = threading.RLock()
        self.log = fun.get_logger()

    def run(self):
        if self.scheduler is None or not bool(
            getattr(self.scheduler, "running", False)
        ):
            raise RuntimeError("scheduler is not running")
        with self._run_lock:
            previous_ids = list(self.task_ids)
            desired_ids = []
            task_list = self.task_list()
            for _t in task_list:
                if _t.is_run != 1:
                    continue

                interval_minutes = max(int(_t.interval_minutes or 1), 1)
                job_id = str(_t.id)
                _job = self.scheduler.add_job(
                    func=self.alert_run,
                    trigger=IntervalTrigger(minutes=interval_minutes),
                    args=(_t.id,),
                    id=job_id,
                    name=f"监控-{_t.task_name}",
                    max_instances=1,
                    coalesce=True,
                    replace_existing=True,
                )
                desired_ids.append(_job.id)

            # Only remove obsolete jobs after every desired job was reconciled.
            for job_id in set(previous_ids) - set(desired_ids):
                try:
                    self.scheduler.remove_job(job_id)
                except JobLookupError:
                    pass
            self.task_ids = desired_ids
        return True

    def alert_run(self, alert_id):
        # Chart rendering is optional and pulls in pyecharts. Keep it outside
        # module import so the core Web application starts without [charts].
        from chanlun import monitor

        alert_config = self.alert_get(alert_id)
        if alert_config is None:
            # 任务被并发删除(alert_del→task_delete)后,本轮已被 default 池调度的触发仍会进来,
            # alert_get 返回 None → 原 Market(None.market) 抛 AttributeError(审查 F1)。
            # 视为"任务已删除"静默跳过,不刷无意义的 AttributeError 噪音。
            return True
        ex = get_exchange(Market(alert_config.market))
        if market_now_trading(ex, alert_config.market) is False:
            return True

        zx = ZiXuan(alert_config.market)
        # 获取自选股票
        stocks = zx.zx_stocks(alert_config.zx_group)
        self.log.info(
            f"执行 {alert_config.task_name} 警报提醒，获取 {alert_config.zx_group} 自选组中 {len(stocks)} 数量股票"
        )
        failures = []
        for s in tqdm(stocks):
            stock_code = (
                str(s.get("code") or "<missing-code>")
                if isinstance(s, dict)
                else "<invalid-stock>"
            )
            try:
                s: Dict[str, str] = s
                cl_config = query_cl_chart_config(alert_config.market, s["code"])
                monitor.monitoring_code(
                    alert_config.task_name,
                    alert_config.market,
                    s["code"],
                    s["name"],
                    [alert_config.frequency],
                    check_cl_types={
                        "bi_types": alert_config.check_bi_type.split(","),
                        "bi_beichi": alert_config.check_bi_beichi.split(","),
                        "bi_mmd": alert_config.check_bi_mmd.split(","),
                        "xd_types": alert_config.check_xd_type.split(","),
                        "xd_beichi": alert_config.check_xd_beichi.split(","),
                        "xd_mmd": alert_config.check_xd_mmd.split(","),
                    },
                    check_idx_types={
                        "idx_ma": (
                            json.loads(alert_config.check_idx_ma_info)
                            if alert_config.check_idx_ma_info
                            else {"enable": 0}
                        ),
                        "idx_macd": (
                            json.loads(alert_config.check_idx_macd_info)
                            if alert_config.check_idx_macd_info
                            else {"enable": 0}
                        ),
                    },
                    is_send_msg=bool(alert_config.is_send_msg),
                    cl_config=cl_config,
                )
            except Exception as e:
                failures.append((stock_code, str(e)))
                self.log.error(f"run {stock_code} alert exception {e}")

        if failures:
            details = "; ".join(f"{code}: {error}" for code, error in failures)
            summary = (
                f"alert task {alert_config.task_name} stock failures "
                f"{len(failures)}/{len(stocks)}: {details}"
            )
            if stocks and len(failures) == len(stocks):
                raise RuntimeError(summary)
            self.log.warning(summary)

        return True

    @staticmethod
    def task_list(market: str = None) -> List[TableByAlertTask]:
        """
        获取警报列表
        """
        alert_list = db.task_query(market=market)
        return alert_list

    @staticmethod
    def alert_get(_id) -> TableByAlertTask:
        # task_query(id=...) 在 db 层已经显式 limit(1)；这里用 next + iter 安全取首元素，
        # 避免 alert_config[0] 在某些边界（如外部并发删除导致空 list）下抛 IndexError。
        alert_config = db.task_query(id=_id)
        if not alert_config:
            return None
        return alert_config[0]

    def alert_save(self, alert_config: Dict):
        """
        添加一个警报
        """
        if self.scheduler is None or not bool(
            getattr(self.scheduler, "running", False)
        ):
            raise RuntimeError("scheduler is not running")
        if alert_config["id"] == "":
            del alert_config["id"]
            # (market, task_name) 去重：应用层 query-first 让普通重复保存转为更新；
            # DB 启动迁移建立的物理唯一索引负责关闭并发同名新建竞态。
            existing = next(
                (
                    t
                    for t in db.task_query(market=alert_config["market"])
                    if t.task_name == alert_config["task_name"]
                ),
                None,
            )
            if existing is not None:
                alert_config["id"] = existing.id
                db.task_update(**alert_config)
            else:
                db.task_save(**alert_config)
        else:
            alert_config["id"] = int(alert_config["id"])
            db.task_update(**alert_config)

        # 重新运行新的监控
        self.run()
        return True

    def alert_del(self, alert_id):
        """
        删除一个警报
        """
        if self.scheduler is None or not bool(
            getattr(self.scheduler, "running", False)
        ):
            raise RuntimeError("scheduler is not running")
        db.task_delete(alert_id)
        self.run()
        return True


if __name__ == "__main__":
    at = AlertTasks(None)

    ls = at.task_list("a")

    print(ls)
