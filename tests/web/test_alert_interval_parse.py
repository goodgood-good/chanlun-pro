"""R5-H2-2: /alert_save 的 interval_minutes 前端 layui number 校验基于 isNaN 放行 '5.5'/'1e3'
(小数/科学计数), 原始字符串到后端裸 int() 会 ValueError→alert_save 无 try→Flask 500(ajax 无
error 回调故用户无提示、任务未保存)。_parse_interval_minutes 稳健解析+clamp 1-1380 不再 500。"""

from cl_app.blueprints.alert import _parse_interval_minutes


def test_normal_int():
    assert _parse_interval_minutes("5") == 5
    assert _parse_interval_minutes("60") == 60


def test_decimal_and_scientific_no_crash():
    # 前端 isNaN 放行的形态: 旧码 int('5.5')/int('1e3') 抛 ValueError→500
    assert _parse_interval_minutes("5.5") == 5
    assert _parse_interval_minutes("1e3") == 1000
    assert _parse_interval_minutes("5.0") == 5


def test_clamp_range():
    assert _parse_interval_minutes("99999") == 1380  # 上界
    assert _parse_interval_minutes("0") == 1  # 下界
    assert _parse_interval_minutes("-5") == 1


def test_invalid_returns_default():
    assert _parse_interval_minutes("abc") == 60
    assert _parse_interval_minutes(None) == 60
    assert _parse_interval_minutes("") == 60
    assert _parse_interval_minutes("  ") == 60