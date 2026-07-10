"""/set_cl_config 缺键健壮性(新R6-W1): 前端 save_cl_config 的 jQuery $.each 校验用 return false
只中断迭代不退出函数(jQuery 语义), 取消勾选全部中枢类型时仍 POST 一份缺键表单。服务端原用裸
request.form[_k](options.py:101)对缺键抛 werkzeug BadRequestKeyError→HTTP 400, ajax 无 error
回调→保存静默失败。修复=_build_cl_config 用 form.get 兜底缺键, 中枢类型全空则干净拒绝(保
"必须选择一个"语义, 不落库退化配置)。可达=web9900「缠论配置项」菜单默认UI。"""

from cl_app.blueprints.options import _build_cl_config


def test_missing_intermediate_key_defaults_zero_no_keyerror():
    # 前端 gotcha 漏发 kline_qk 等中间键 → 兜底 "0", 不抛 KeyError(旧码此处 400)
    keys = ["config_use_type", "kline_qk", "zs_bi_type", "zs_xd_type"]
    form = {"config_use_type": "common", "zs_bi_type": "bi", "zs_xd_type": "xd"}
    cfg, err = _build_cl_config(form, keys)
    assert err is None
    assert cfg["kline_qk"] == "0"
    assert cfg["zs_bi_type"] == ["bi"]


def test_empty_bi_type_rejected_cleanly():
    # 取消勾选全部笔中枢类型 → 干净拒绝(非 400/非落库退化配置)
    keys = ["zs_bi_type", "zs_xd_type"]
    form = {"zs_xd_type": "xd"}  # zs_bi_type 缺
    cfg, err = _build_cl_config(form, keys)
    assert cfg is None
    assert "笔中枢类型" in err


def test_empty_xd_type_rejected_cleanly():
    keys = ["zs_bi_type", "zs_xd_type"]
    form = {"zs_bi_type": "bi", "zs_xd_type": ""}  # 显式空串
    cfg, err = _build_cl_config(form, keys)
    assert cfg is None
    assert "线段中枢类型" in err


def test_valid_config_unchanged_behavior():
    # 防呆: 有效配置(两类型都选)行为与旧码一致(split 成列表, 空值键→"0")
    keys = ["config_use_type", "fx_qy", "zs_bi_type", "zs_xd_type"]
    form = {
        "config_use_type": "common",
        "fx_qy": "",  # 空值键 → "0"
        "zs_bi_type": "bi,dd",
        "zs_xd_type": "xd",
    }
    cfg, err = _build_cl_config(form, keys)
    assert err is None
    assert cfg["config_use_type"] == "common"
    assert cfg["fx_qy"] == "0"
    assert cfg["zs_bi_type"] == ["bi", "dd"]
    assert cfg["zs_xd_type"] == ["xd"]
