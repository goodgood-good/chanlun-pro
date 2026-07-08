"""R1-C3: xg_single_bi_1mmd/2mmd/3mmd 做空模式恒 0 结果修复的守护网。

三函数曾写死找 1buy/2buy/3buy 且从不使用 get_opt_types 返回的 opt_mmd,
而 core 按笔方向挂载信号(up 笔只挂 sell 类), 做空(opt_direction=[up])
下条件恒假 → 全市场恒 0 命中, 随后任务无条件清空目标自选组=数据破坏。
修复口径: 类别对(1buy,1sell)交 opt_mmd 过滤, 方向语义与兄弟函数一致。
"""
import pytest

from chanlun.xuangu import xuangu


class _Zs:
    def __init__(self, line_num):
        self.line_num = line_num


class _Mmd:
    def __init__(self, name, line_num=5):
        self.name = name
        self.zs = _Zs(line_num)


class _Bi:
    def __init__(self, type_, mmds):
        self.type = type_
        self.zs_type_mmds = {"zsd": list(mmds)}


class _Cd:
    def __init__(self, bi):
        self._bi = bi

    def get_bis(self):
        return [self._bi]

    def get_code(self):
        return "SH.TEST"

    def get_frequency(self):
        return "5m"


class _Datas:
    frequencys = ["5m"]

    def __init__(self, cd):
        self._cd = cd

    def get_cl_data(self, code, frequency):
        return self._cd


CASES = [
    pytest.param(xuangu.xg_single_bi_1mmd, "1buy", "1sell", id="1mmd"),
    pytest.param(xuangu.xg_single_bi_2mmd, "2buy", "2sell", id="2mmd"),
    pytest.param(xuangu.xg_single_bi_3mmd, "3buy", "3sell", id="3mmd"),
]


@pytest.mark.parametrize("fn,buy_name,sell_name", CASES)
def test_short_mode_finds_sell_point_on_up_bi(fn, buy_name, sell_name):
    # 做空: up 笔挂 Nsell → 必须命中(旧实现写死找 Nbuy 恒 None)
    datas = _Datas(_Cd(_Bi("up", [_Mmd(sell_name)])))
    res = fn("SH.TEST", datas, ["short"])
    assert res is not None
    assert sell_name in res["msg"]


@pytest.mark.parametrize("fn,buy_name,sell_name", CASES)
def test_long_mode_still_finds_buy_point_on_down_bi(fn, buy_name, sell_name):
    # 做多回归保护: down 笔挂 Nbuy → 命中不变
    datas = _Datas(_Cd(_Bi("down", [_Mmd(buy_name)])))
    res = fn("SH.TEST", datas, ["long"])
    assert res is not None


@pytest.mark.parametrize("fn,buy_name,sell_name", CASES)
def test_short_mode_rejects_down_bi(fn, buy_name, sell_name):
    # 做空下 down 笔被方向过滤拒绝
    datas = _Datas(_Cd(_Bi("down", [_Mmd(buy_name)])))
    assert fn("SH.TEST", datas, ["short"]) is None


@pytest.mark.parametrize("fn,buy_name,sell_name", CASES)
def test_line_num_gate_preserved(fn, buy_name, sell_name):
    # 中枢 line_num >= 9 的既有门槛保持
    datas = _Datas(_Cd(_Bi("up", [_Mmd(sell_name, line_num=9)])))
    assert fn("SH.TEST", datas, ["short"]) is None