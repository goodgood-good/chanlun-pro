"""D3-H1 回归: tdx_us / tdx_hk 的 klines() 守卫不得因 start_date/end_date 非空而 return None。

to_tdx_code 恒返回非 None market(us/hk 固定 74), 故原守卫
`if market is None or start_date is not None or end_date is not None: return None`
退化为「传 end_date 即 return None」; web 主加载路径必传 end_date=now -> @retry 3 次 None ->
RetryError -> 美股/港股图表恒空。tdx_fx 已修为 `if market is None:`(d25ce69e), us/hk 漏网。
守卫后从不读 start_date/end_date(仅按 pages 拉最新由上层裁剪), 忽略范围参数安全。
klines 依赖联网无法离线行为测试, 故用源码守卫回归锁死这一确定性缺陷。
"""
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FILES = [
    "src/chanlun/exchange/exchange_tdx_us.py",
    "src/chanlun/exchange/exchange_tdx_hk.py",
]


def test_tdx_us_hk_klines_guard_ignores_range_params():
    for rel in _FILES:
        txt = (_ROOT / rel).read_text(encoding="utf-8")
        assert "start_date is not None or end_date is not None" not in txt, (
            f"{rel}: klines 守卫仍因 start_date/end_date return None -> web 传 end_date 时 "
            f"RetryError 图表恒空; 应对齐 tdx_fx 改为 if market is None。"
        )