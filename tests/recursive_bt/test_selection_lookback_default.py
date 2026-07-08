"""Round11 B2: 选股回看窗 lookback_bars 代码默认曾为 3(15分钟), 重现"盘后启动永远选不出候选"
(买点早于最后3根5m bar即被静默丢弃)。生产 config selection_lookback_bars=48 已覆盖, 但代码
默认仍是 3 = latent footgun(任何不覆盖该 config 的部署/新市场分支会重蹈)。锁定默认>=48。"""

from chanlun.recursive_bt.select.chanlun_selector import ASelectionConfig


def test_a_selection_config_lookback_default_safe():
    cfg = ASelectionConfig(bt_data="x")
    # 默认必须 >= 48(约一交易日的5m bar数), 不得回退到会致盘后漏选的 3
    assert cfg.lookback_bars >= 48