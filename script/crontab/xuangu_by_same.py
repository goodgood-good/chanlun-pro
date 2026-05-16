#:  -*- coding: utf-8 -*-
"""
相似度选股脚本：给定目标股票，从候选池中找出缠论结构最相似的 N 只股票，加入自选组。
"""
import datetime

from chanlun import fun, zixuan  # noqa: F401
from chanlun.base import Market
from chanlun.cl_utils import query_cl_chart_config
from chanlun.db import db
from chanlun.exchange import get_exchange
from chanlun.xuangu.xuangu_by_same import XuanguBySame

if __name__ == "__main__":
    market = "a"
    # 缠论配置须与前台展示一致，否则选股结果在图表上可能看不到对应形态
    cl_config = query_cl_chart_config(market, "SH.000001")
    zx = zixuan.ZiXuan(market)
    zx_group = "相似选股"
    xg_mark = "SG"

    # 每次运行前清空上次结果，保持自选组和标记点干净
    zx.clear_zx_stocks(zx_group)
    db.marks_del(market=market, mark_label=xg_mark)

    target_code = "SZ.002082"
    target_frequency = "d"
    # 目标截至时间，不设置则使用最新数据
    target_end_datetime = None

    ex = get_exchange(Market(market))
    all_stocks = ex.all_stocks()
    find_codes = [
        _s["code"]
        for _s in all_stocks
        if _s["code"].startswith("SH.60")
        # or _s["code"].startswith("SZ.00")
        # or _s["code"].startswith("SZ.30")
    ]  # 按需增减候选池范围

    xg_by_same = XuanguBySame(market)
    # 相似度各维度参数，权重之和须等于 1；不参与比较的维度权重设 0.0
    xg_by_same.k_num = 200      # K 线比较数量
    xg_by_same.bi_num = 9       # 笔比较数量
    xg_by_same.xd_num = 3       # 线段比较数量
    xg_by_same.k_weight = 0.3   # K 线相似度权重
    xg_by_same.bi_weight = 0.6  # 笔相似度权重
    xg_by_same.xd_weight = 0.1  # 线段相似度权重

    same_codes = xg_by_same.run(
        target_code=target_code,
        frequency=target_frequency,
        target_end_datetime=target_end_datetime,
        find_codes=find_codes,
        cl_config=cl_config,
        run_type="process",
    )

    same_codes = sorted(same_codes, key=lambda x: x["similarity"], reverse=True)
    for _s in same_codes[:20]:
        print(_s)
        zx.add_stock(zx_group, _s["code"], None, memo=_s["similarity"])
        db.marks_add(
            market=market,
            stock_code=_s["code"],
            stock_name=_s["code"],
            frequency="",
            mark_time=fun.datetime_to_int(datetime.datetime.now()),
            mark_label=xg_mark,
            mark_tooltip=f"相似度：{_s['similarity']}",
            mark_shape="circle",
            mark_color="red",
        )

    print("Done")
