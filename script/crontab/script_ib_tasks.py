"""
盈透证券 IB 任务处理进程：监听 Redis 队列，代理执行行情/交易指令。

启动多个客户端进程（每个对应一个 IB clientId），通过 blpop 阻塞等待
search_stocks / klines / ticks / balance / positions / orders 等命令，
执行后将结果 lpush 回调方队列。
"""
import datetime
import json
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

import ib_insync
import pandas as pd
import pytz

from chanlun import config, fun, rd
from chanlun.base import Market
from chanlun.exchange.exchange_ib import CmdEnum, ib_res_hkey
from chanlun.persistence.file_db import FileCacheDB


def run_tasks(client_id: int):
    """
    接收命令，并调用接口获取数据
    """
    log = fun.get_logger("ib_tasks.log")

    ib: ib_insync.IB = ib_insync.IB()
    ib_insync.util.allowCtrlC()

    fdb = FileCacheDB()  # K 线本地缓存，用于增量更新

    tz = pytz.timezone("US/Eastern")

    def get_ib() -> ib_insync.IB:
        if ib.isConnected():
            return ib
        try:
            ib.connect(
                config.IB_HOST,
                config.IB_PORT,
                clientId=client_id,
                account=config.IB_ACCOUNT,
            )
        except Exception as e:
            log.error(f"get ib connect error : {e}")
            time.sleep(10)
            return get_ib()
        return ib

    def search_stocks(search):
        """
        按照关键词搜索
        """
        stocks = get_ib().reqMatchingSymbols(search)
        res = []
        for s in stocks:
            if s.contract.currency == "USD":
                code = get_code_by_contract(s.contract)
                if code != "":
                    res.append({"code": code, "name": s.contract.description})
        return res

    def klines(code, durationStr, barSizeSetting, timeout):
        for i in range(2):
            contract = get_contract_by_code(code)
            history_klines = fdb.get_tdx_klines(
                Market.US.value, f"ib_{code}", barSizeSetting.replace(" ", "")
            )
            new_durationStr = durationStr
            if history_klines is not None and len(history_klines) >= 100:
                # 有本地缓存时仅拉取增量，减少 IB API 请求量
                diff_days = (
                    datetime.datetime.now() - history_klines.iloc[-1]["date"]
                ).days + 30
                new_durationStr = f"{diff_days} D"

            re_request_bars = False  # 末根收盘价不一致时置 True，触发全量重新请求
            bars = get_ib().reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=new_durationStr,
                barSizeSetting=barSizeSetting,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
                timeout=timeout,
            )
            klines_res = []
            for _b in bars:
                if (
                    history_klines is not None
                    and len(history_klines) >= 100
                    and re_request_bars is False
                ):
                    history_last_dt = fun.datetime_to_str(
                        history_klines.iloc[-1]["date"]
                    )
                    if (
                        history_last_dt == fun.datetime_to_str(_b.date)
                        and history_klines.iloc[-1]["close"] != _b.close
                    ):
                        re_request_bars = True
                        break

                klines_res.append(
                    {
                        "code": code,
                        "date": fun.datetime_to_str(_b.date),
                        "open": _b.open,
                        "close": _b.close,
                        "high": _b.high,
                        "low": _b.low,
                        "volume": _b.volume,
                    }
                )
            if len(klines_res) == 0:
                continue

            if re_request_bars:  # 重新进行全量请求
                bars = get_ib().reqHistoricalData(
                    contract,
                    endDateTime="",
                    durationStr=durationStr,
                    barSizeSetting=barSizeSetting,
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                    timeout=timeout,
                )
                klines_res = []
                for _b in bars:
                    klines_res.append(
                        {
                            "code": code,
                            "date": fun.datetime_to_str(_b.date),
                            "open": _b.open,
                            "close": _b.close,
                            "high": _b.high,
                            "low": _b.low,
                            "volume": _b.volume,
                        }
                    )
            else:
                # 增量合并：与本地缓存拼接，去重后按时间排序
                klines_df = pd.DataFrame(klines_res)
                klines_df["date"] = pd.to_datetime(klines_df["date"])
                klines_df = pd.concat([history_klines, klines_df], ignore_index=True)
                klines_df = klines_df.drop_duplicates(
                    ["date"], keep="last"
                ).sort_values("date")
                klines_res = []
                klines_df["date"] = klines_df["date"].apply(
                    lambda d: fun.datetime_to_str(d)
                )
                for _, _k in klines_df.iterrows():
                    klines_res.append(_k.to_dict())

            if len(klines_res) == 0:
                continue
            klines_df = pd.DataFrame(klines_res)
            fdb.save_tdx_klines(
                Market.US.value,
                f"ib_{code}",
                barSizeSetting.replace(" ", ""),
                klines_df,
            )
            return klines_res
        return []

    def ticks(codes):
        contracts = [get_contract_by_code(code) for code in codes]
        tks = get_ib().reqTickers(*contracts)
        res = []
        for tk in tks:
            if tk is None or tk.last != tk.last:
                continue
            res.append(
                {
                    "code": get_code_by_contract(tk.contract),
                    "last": tk.last,
                    "buy1": tk.bid,
                    "sell1": tk.ask,
                    "open": tk.open,
                    "high": tk.high,
                    "low": tk.low,
                    "volume": tk.volume,
                    "rate": round((tk.last - tk.close) / tk.close * 100, 2),
                }
            )
        return res

    def stock_info(code):
        contract = get_contract_by_code(code)
        details = get_ib().reqContractDetails(contract)
        if len(details) == 0:
            return None
        return {"code": code, "name": details[0].longName}

    def balance():
        account = get_ib().accountSummary(account=config.IB_ACCOUNT)
        info = {_a.tag: float(_a.value) for _a in account if _a.currency == "USD"}
        return info

    def positions(code: str = ""):
        hold_positions = get_ib().positions(account=config.IB_ACCOUNT)
        hold_positions = [
            {
                "code": get_code_by_contract(_p.contract),
                "account": _p.account,
                "avgCost": _p.avgCost,
                "position": _p.position,
            }
            for _p in hold_positions
        ]

        if code != "":
            for _p in hold_positions:
                if _p["code"] == code:
                    return _p
            return None
        return hold_positions

    def orders(code, type, amount):
        contract = get_contract_by_code(code)
        if type == "buy":
            req_order = ib_insync.MarketOrder("BUY", amount)
        else:
            req_order = ib_insync.MarketOrder("SELL", amount)

        trade = get_ib().placeOrder(contract, req_order)
        while True:
            get_ib().sleep(1)
            if trade.isDone():
                break
        return {
            "price": trade.orderStatus.avgFillPrice,
            "amount": trade.orderStatus.filled,
        }

    def get_contract_by_code(code: str):
        """根据内部 code 格式构建 IB 合约对象（股票/指数/期货/加密货币）。"""
        if "_IND_" in code:
            return ib_insync.Index(
                symbol=code.split("_")[0], exchange=code.split("_")[2], currency="USD"
            )
        elif "_FUT_" in code:
            return ib_insync.Future(
                symbol=code.split("_")[0], exchange=code.split("_")[2], currency="USD"
            )
        elif "_CRYPTO_" in code:
            return ib_insync.Crypto(
                symbol=code.split("_")[0], exchange=code.split("_")[2], currency="USD"
            )
        else:
            contract = ib_insync.Stock(symbol=code, exchange="SMART", currency="USD")
            # primaryExchange 缓存在 Redis，避免每次重复调用 reqContractDetails
            primaryExchange = rd.Robj().hget("us_contract_details", code)
            if primaryExchange is None:
                details = get_ib().reqContractDetails(contract)
                if len(details) > 0:
                    for d in details:
                        if d.contract.currency == "USD":
                            primaryExchange = d.contract.primaryExchange
                            rd.Robj().hset("us_contract_details", code, primaryExchange)
                            break
            if primaryExchange is not None:
                contract.primaryExchange = primaryExchange
            return contract

    def get_code_by_contract(contract: ib_insync.Contract):
        if contract.secType == "STK":
            return contract.symbol
        elif contract.secType == "IND":  # 指数添加 .IND 后缀
            return f"{contract.symbol}_IND_{contract.primaryExchange}"
        elif contract.secType == "FUT":  # 期货添加 .FUT 后缀
            return f"{contract.symbol}_FUT_{contract.primaryExchange}"
        elif contract.secType == "CRYPTO":  # 加密货币添加 .CRYPTO 后缀
            return f"{contract.symbol}_CRYPTO_{contract.primaryExchange}"
        return ""

    while True:
        cmd: str = ""
        args: str = ""
        res = None
        try:
            cmd, args = rd.Robj().blpop(
                [
                    CmdEnum.SEARCH_STOCKS.value,
                    CmdEnum.KLINES.value,
                    CmdEnum.TICKS.value,
                    CmdEnum.STOCK_INFO.value,
                    CmdEnum.BALANCE.value,
                    CmdEnum.POSITIONS.value,
                    CmdEnum.ORDERS.value,
                ],
                0,
            )
            s_time = time.time()
            args: dict = json.loads(args)
            info = ""
            if cmd == CmdEnum.SEARCH_STOCKS.value:
                info = args["search"]
                res = search_stocks(args["search"])
            elif cmd == CmdEnum.KLINES.value:
                info = (
                    f"{args['code']} - {args['durationStr']} - {args['barSizeSetting']}"
                )
                res = klines(
                    args["code"],
                    args["durationStr"],
                    args["barSizeSetting"],
                    args["timeout"],
                )
            elif cmd == CmdEnum.TICKS.value:
                info = args["codes"]
                res = ticks(args["codes"])
            elif cmd == CmdEnum.STOCK_INFO.value:
                info = args["code"]
                res = stock_info(args["code"])
            elif cmd == CmdEnum.BALANCE.value:
                info = "balance"
                res = balance()
            elif cmd == CmdEnum.POSITIONS.value:
                info = args["code"]
                res = positions(args["code"])
            elif cmd == CmdEnum.ORDERS.value:
                info = args["code"]
                res = orders(args["code"], args["type"], args["amount"])

            rd.Robj().lpush(args["key"], json.dumps(res))
            log.info(
                f"{client_id} Task CMD {cmd} [ {info} ] run times : {time.time() - s_time}"
            )
        except Exception as e:
            log.error(f"{client_id} Task CMD {cmd} args {args} ERROR {e}")
            log.error(traceback.format_exc())


if __name__ == "__main__":
    print("Start Run")

    # 启动前清空残留队列和结果，防止旧数据干扰
    for _k in [
        CmdEnum.SEARCH_STOCKS.value,
        CmdEnum.KLINES.value,
        CmdEnum.TICKS.value,
        CmdEnum.STOCK_INFO.value,
        CmdEnum.BALANCE.value,
        CmdEnum.POSITIONS.value,
        CmdEnum.ORDERS.value,
    ]:
        rd.Robj().delete(_k)

    h_keys = rd.Robj().keys(f"{ib_res_hkey}*")
    for _k in h_keys:
        rd.Robj().delete(_k)

    # 启动 5 个客户端进程，clientId 21-25 各自独立连接 IB
    start_client_num = 5
    with ProcessPoolExecutor(
        start_client_num, mp_context=get_context("spawn")
    ) as executor:
        executor.map(run_tasks, [21, 22, 23, 24, 25])
