"""tv_history 后端周期闸门 + 季线 q 暴露面的固化测试(2026-06-23)。

背景: 为外汇季线加了 frequency_maps['q']='3M' 后, 在 tv_history 加后端闸门
(frequency 必须在 market 的 support_frequencys 内, 否则 no_data), 防手构请求
绕过前端 supported_resolutions 把不支持周期(如 cq 美股季线 q)传到 exchange。

本测试固化闸门的两个依据 + 不误拦不变量, 任一处被意外改动会被抓:
  1. resolution_maps['3M'] 反查得 'q'(q 改动本身);
  2. 季线 q 只在 fx(tdx_fx support 含 q)暴露, 其余 7 个路由 market 都不暴露;
  3. 正常周期 1m/5m 在所有 market 内(否则闸门会误拦正常请求)。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))


def test_resolution_3m_maps_to_quarter_q():
    from cl_app.services.constants import frequency_maps, resolution_maps

    assert frequency_maps.get("q") == "3M"
    assert resolution_maps.get("3M") == "q"


def test_only_fx_exposes_quarter_q():
    """季线 q 仅应在 fx 暴露(tdx_fx support 含 q); 其余 market 不含 → 后端闸门拒绝手构 q。"""
    from cl_app.services.constants import market_frequencys

    assert "q" in market_frequencys.get("fx", []), "外汇应暴露季线 q"
    for m in ("us", "a", "hk", "futures", "ny_futures", "currency", "currency_spot"):
        assert "q" not in market_frequencys.get(m, []), f"{m} 不应暴露季线 q(手构 q 须被闸门拒绝)"


def test_normal_freqs_present_in_all_markets():
    """正常周期 1m/5m 必须在各 market 的 support 内, 否则后端闸门会把正常请求误拦成 no_data。"""
    from cl_app.services.constants import market_frequencys

    for m in ("us", "a", "hk", "fx", "futures"):
        fs = market_frequencys.get(m, [])
        assert "5m" in fs and "1m" in fs, f"{m} 缺正常周期 1m/5m → 闸门会误拦正常切换"
