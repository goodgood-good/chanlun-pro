"""chanlun.recursive_bt — 缠论递归多级别选股与回测系统。

基于重做的新核心(chanlun.core: zs_branch/zslx_branch/recursive_branch/bs_branch)构建的
选股交易体系与回测框架。模块:
- engine        : 单标的多级别回测引擎(数据/信号/市场规则/撮合/大小级别结合策略)
- portfolio     : 买点选股 + 组合回测(并发持仓/T+1/涨跌停/大盘择时过滤)
- systems       : 缠论「三个独立的系统」(基本面+比价+技术面,概率原则)
- fetch         : QMT 拉前复权 K线 + 预计算缠论信号 → bt_data 缓存
- fundamentals  : QMT 拉基本面(ROE/成长,公告日 point-in-time) + 市值 → bt_data_fund 缓存
- validate      : 真·walk-forward 抽样验证(实盘真实性)

运行入口(PYTHONPATH=src):
    python -m chanlun.recursive_bt.data.fetch 沪深300       # 取数
    python -m chanlun.recursive_bt.engine.fundamentals        # 取基本面
    python -m chanlun.recursive_bt.sim.portfolio           # 全市场选股组合回测
    python -m chanlun.recursive_bt.select.systems             # 三个独立系统组合
    python -m chanlun.recursive_bt.backtest.validate            # 实盘真实性验证
"""
