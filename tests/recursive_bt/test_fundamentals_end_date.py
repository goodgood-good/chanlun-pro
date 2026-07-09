"""fundamentals.py 报告期上界必须动态取当前日期(不得硬编码近过去日)。

硬编码 "20260601" 时,report_type="report_time" 下按报告期(m_timetag)过滤,
period > 2026-06-01 的报告(Q2 2026 起 m_timetag=2026-06-30)永不返回,
即便手工删 pkl 全量重抓也拿不到 → fund_ok/成长门长期用陈旧季报放行。
Round8 #4 回归网。
"""
import datetime as _dt
import sys
import types



def _make_xt(recorded):
    """构造最小 xtquant 桩: 记录 get_financial_data 的入参, 返回空数据。"""
    xtdata = types.SimpleNamespace()
    xtdata.download_financial_data = lambda *a, **k: None

    def _gfd(qcodes, fields, start, end, report_type=None):
        recorded.append({"start": start, "end": end, "report_type": report_type})
        return {}

    xtdata.get_financial_data = _gfd
    xtdata.get_instrument_detail = lambda qc: {}   # 返回 falsy → 不触发 _f(TotalVolume)
    mod = types.ModuleType("xtquant")
    mod.xtdata = xtdata
    return mod


def test_fund_end_date_is_dynamic():
    from chanlun.recursive_bt.engine import fundamentals

    end = fundamentals._fund_end_date()
    assert len(end) == 8 and end.isdigit(), f"格式应为 YYYYMMDD, 实际={end!r}"
    # 动态取当前日期 → 必然等于今天, 且晚于旧硬编码上界 "20260601"
    assert end == _dt.datetime.now().strftime("%Y%m%d")
    assert int(end) > 20260601


def test_fetch_passes_dynamic_end_date(monkeypatch, tmp_path):
    from chanlun.recursive_bt.engine import fundamentals

    recorded = []
    monkeypatch.setitem(sys.modules, "xtquant", _make_xt(recorded))
    fundamentals.fetch(["SH.600519"], out_dir=str(tmp_path))
    assert recorded, "get_financial_data 未被调用"
    assert recorded[0]["start"] == "20210101"
    assert recorded[0]["report_type"] == "report_time"
    assert int(recorded[0]["end"]) > 20260601   # 非硬编码 20260601


def test_fetch_batched_passes_dynamic_end_date(monkeypatch, tmp_path):
    from chanlun.recursive_bt.engine import fundamentals

    recorded = []
    monkeypatch.setitem(sys.modules, "xtquant", _make_xt(recorded))
    fundamentals.fetch_batched(["SH.600519"], out_dir=str(tmp_path))
    assert recorded, "get_financial_data 未被调用"
    assert recorded[0]["start"] == "20210101"
    assert recorded[0]["report_type"] == "report_time"
    assert int(recorded[0]["end"]) > 20260601