from copy import deepcopy

from cl_app.services.notification_text import signal_content, price
from cl_app.services.signal_monitor import _invalidation_evidence, _signal


def warning():
    return {"id":"test","market":"currency_spot","code":"BTC/USDT","name":"BTC/USDT",
            "frequency":"5m","signal_type":"consolidation_divergence","point_type":"consolidation_up",
            "stage":"warning","point_status":"warning","anchor_price":84921.7,"latest_close":85000.,
            "structure_price_quantum":"0.1","macd_area_a":2637.724204452031,"macd_area_c":788.239694375505,
            "macd_area_ratio":788.239694375505/2637.724204452031,"source_closed_at":1791031800,
            "first_warned_at":1791031812,"recorded_at":1791031812,"holding":True,
            "comparison_form":"entry_departure","hypothesis_id":"hyp","terminal_start_at":100,
            "price_basis_revision":"raw"}


def test_concise_preview_has_named_price_ratio_clock_and_no_page_link():
    row=warning();text=signal_content([row],1791031813)
    assert text.startswith('【提前预警】BTC/USDT · 5m')
    assert 'currency_spot:' not in text and '2637.724204452031' not in text
    assert '29.9%' in text and '候选拐点：84,921.7' in text
    assert '最新5m收盘：85,000.0' in text
    assert '首次预警：10-03 20:50:12' in text and '消息生成：10-03 20:50:13' in text
    assert 'http://' not in text and 'https://' not in text and '查看当前图表' not in text


def test_invalidation_uses_current_reason_and_does_not_relabel_old_area_as_current():
    previous=warning()
    period={"source_closed_at":1791032400,"latest_close":85100.,"consolidation_checks":{
        "schema":"native-preview-checks-v1","source_closed_at":1791032400,"price_basis_revision":"raw",
        "tail_start_at":100,"reason_code":"MOMENTUM_RECOVERED","evaluations":{}}}
    evidence=_invalidation_evidence(previous,period)
    assert evidence['macd_area_a'] is None and evidence['macd_area_c'] is None
    assert evidence['last_valid_at']==previous['source_closed_at']
    row={**previous,**evidence,"stage":"invalidated","point_status":"invalidated",
         "state_changed_at":1791032411,"source_closed_at":1791032400}
    text=signal_content([row],1791032411)
    assert '同向MACD柱在收缩后重新扩大' in text
    assert 'MACD 同向柱面积' not in text
    assert '原预警参考价：84,921.7' in text and '对应预警：10-03 20:50:12' in text
    assert '9分59秒' in text and '失效复核：10-03 21:00:00' in text


def test_diagnostic_from_another_cutoff_is_not_used_for_a_specific_reason():
    previous=warning()
    result=_invalidation_evidence(previous,{"source_closed_at":200,"consolidation_checks":{
        "schema":"native-preview-checks-v1","source_closed_at":199,"reason_code":"MOMENTUM_RECOVERED"}})
    assert result['invalidation_reason_code']=='DIAGNOSTIC_UNAVAILABLE'


def test_buy_point_carries_verified_stop_reference_and_original_clock():
    candidate={"market":"us","code":"NVDA.US","name":"英伟达","frequency":"5m","source_closed_at":1000,
               "latest_close":229.5,"structure_price_quantum":"0.001","evidence":{"status":"complete"},
               "point":{"point_id":"p","point_type":"3buy","status":"confirmed","anchor_price":227.692,
                        "anchor_at":700,"confirmed_at":900,"available_at":900},
               "exit_plan":{"stop_loss":{"price":226.115,"status":"reference","trigger":"lt"},
                            "take_profit":{"status":"waiting","price":None}}}
    original=deepcopy(candidate);row={**_signal(candidate),"recorded_at":1000}
    text=signal_content([row],1000)
    assert '结构拐点：227.692' in text and '结构止损参考：跌破226.115' in text
    assert '等待本周期已确认反向信号' in text and candidate==original
    assert price(float('nan'))=='—' and price(1.234,'0.001')=='1.234'
