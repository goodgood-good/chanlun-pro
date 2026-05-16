import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from chanlun.exchange.stocks_bkgn import StocksBKGN


class OtherTasks:
    def __init__(self, scheduler: BackgroundScheduler):
        self.scheduler = scheduler

        self.stock_bkgn = StocksBKGN()

        self.run_task()

    def run_task(self):
        # 行业/概念板块数据推荐通过 config.TDX_PATH 读取本地通达信文件获取，
        # 定时任务按需在此注册（示例见历史版本）。
        pass
