"""Offline coverage for independent notification history retrieval."""
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from cl_app.services import notification_history as history

from chanlun import config
from chanlun.exchange.exchange_usmart import ExchangeUSmart, _timestamp_to_api_time
from chanlun.exchange.price_basis import build_provider_price_basis_metadata, attach_price_basis_metadata
from decimal import Decimal


@pytest.fixture(autouse=True)
def native_config(monkeypatch):
    monkeypatch.setattr(config, "EXCHANGE_US", "usmart")
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "usmart")


def raw_page(stamps, frequency="1m", **overrides):
    result = {"market": "us", "symbol": "NVDA", "type": {"1m": 1, "5m": 2, "30m": 5}[frequency],
              "right": 1, "list": [dict(latestTime=_timestamp_to_api_time(pd.Timestamp(stamp).tz_convert("US/Eastern")),
              open=10., high=12., low=9., close=11., volume=100.) for stamp in stamps]}
    result.update(overrides)
    return result


def source_frame(stamps, frequency="1m", **attrs):
    frame = pd.DataFrame({"date": pd.to_datetime(stamps, utc=True), "code": "NVDA.US", "frequency": frequency,
                          "open": 10., "high": 12., "low": 9., "close": 11., "volume": 100.})
    frame = attach_price_basis_metadata(frame, build_provider_price_basis_metadata(
        provider="usmart", market="us", code="NVDA.US", adjustment="forward",
        structure_price_quantum=Decimal("0.001")))
    frame.attrs.update(attrs)
    return frame


class Pages:
    identity = "offline-usmart"

    def __init__(self, *pages):
        self.pages, self.calls = list(pages), []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def args(frequency="1m", anchor=None):
    cutoff = pd.Timestamp("2026-10-09 20:00Z")
    anchor = source_frame([cutoff-pd.Timedelta(minutes=int(frequency[:-1])), cutoff], frequency) if anchor is None else anchor
    event = {"id": "old-event", "market": "us", "code": "NVDA.US", "frequency": "1m",
             "source_closed_at": int(cutoff.timestamp())}
    return event, frequency, dict(anchor.attrs), dict(anchor_frame=anchor, aligned_cutoff=int(cutoff.timestamp()))


def run(inputs, **kwargs):
    return history.prepare_notification_history(*inputs[:3], **inputs[3], **kwargs)


@pytest.mark.parametrize("frequency,days", [("1m", 30), ("5m", 90)])
def test_finite_targets_need_real_earlier_pages_and_leave_event_anchor_unchanged(frequency, days):
    inp = args(frequency)
    event, anchor = deepcopy(inp[0]), inp[3]["anchor_frame"].copy(deep=True)
    cutoff = anchor.date.max()
    page1 = raw_page(anchor.date, frequency)
    page2 = raw_page([cutoff-pd.Timedelta(days=days), cutoff-pd.Timedelta(days=days-1)], frequency)
    pages = Pages(page1, page2)
    result = run(inp, page_source=pages)
    assert result.coverage["reason"] == "target_reached"
    assert result.coverage["target_reached"] and not result.coverage["source_origin"]
    assert result.coverage["anchor_complete"] and result.coverage["consumer_validation_required"]
    assert len(result.frame) == 4
    assert pages.calls[1]["cursor"] == anchor.date.min()-pd.Timedelta(milliseconds=1)
    assert pages.calls[0]["right"] == 1
    assert inp[0] == event
    pd.testing.assert_frame_equal(inp[3]["anchor_frame"], anchor)
    assert inp[3]["anchor_frame"].attrs == anchor.attrs
    assert result.frame.attrs["price_basis_revision"] == anchor.attrs["price_basis_revision"]


def test_provider_origin_requires_successful_empty_page_and_is_not_arbitrary_two_years():
    inp = args("30m")
    early = pd.Timestamp("2006-01-04 21:00Z")
    pages = Pages(raw_page(inp[3]["anchor_frame"].date, "30m"), raw_page([early], "30m"),
                  raw_page([], "30m", list=None))
    result = run(inp, page_source=pages)
    assert result.coverage["reason"] == "source_origin" and result.coverage["source_origin"]
    assert result.coverage["requested_from"] is None
    assert result.frame.date.min() == early
    assert result.state["proofs"][-1]["rows"] == 0
    assert result.state["proofs"][-1]["before"] == (early-pd.Timedelta(milliseconds=1)).isoformat()


@pytest.mark.parametrize("frequency", ["1m", "5m"])
def test_short_provider_origin_does_not_pretend_target_satisfied(frequency):
    inp = args(frequency)
    result = run(inp, page_source=Pages(raw_page(inp[3]["anchor_frame"].date, frequency), raw_page([], frequency)))
    assert result.coverage["source_origin"] and not result.coverage["target_reached"]
    assert result.coverage["reason"] == "source_origin" and not result.coverage["can_resume"]


def test_page_budget_resume_is_deterministic_and_never_changes_previous_result():
    inp = args("30m")
    pages = Pages(raw_page(inp[3]["anchor_frame"].date, "30m"), raw_page([], "30m"))
    first = run(inp, page_source=pages, max_pages=1)
    before = deepcopy(first)
    assert first.coverage["reason"] == "page_budget" and first.coverage["can_resume"]
    assert not first.coverage["source_origin"]
    second = run(inp, page_source=pages, previous=first, max_pages=1)
    assert second.coverage["source_origin"] and second.coverage["pages"] == 2
    assert first.state == before.state and first.coverage == before.coverage
    pd.testing.assert_frame_equal(first.frame, before.frame)
    third = run(inp, page_source=Pages(), previous=second)
    assert third.coverage["source_origin"]


def test_deadline_keeps_safe_cursor_and_does_not_issue_another_page():
    inp = args("30m")
    current = [100.]
    pages = Pages(raw_page(inp[3]["anchor_frame"].date, "30m"))
    def fetch(**kwargs):
        result = pages(**kwargs)
        current[0] += 21
        return result
    fetch.identity = pages.identity
    result = run(inp, page_source=fetch, monotonic=lambda:current[0], timeout_seconds=20)
    assert result.coverage["reason"] == "deadline"
    assert result.coverage["can_resume"] and not result.coverage["source_origin"]
    assert len(pages.calls) == 1


def test_http_error_is_resumable_and_not_empty_origin_or_secret_leak():
    inp = args("30m")
    pages = Pages(raw_page(inp[3]["anchor_frame"].date, "30m"), RuntimeError("token=secret"))
    result = run(inp, page_source=pages)
    assert result.coverage["reason"] == "source_error" and result.coverage["can_resume"]
    assert result.coverage["error_code"] == "RuntimeError"
    assert not result.coverage["source_origin"] and "secret" not in str(result)
    resume = run(inp, page_source=Pages(raw_page([], "30m")), previous=result)
    assert resume.coverage["source_origin"]


@pytest.mark.parametrize("change", ["market", "symbol", "type", "right", "missing_list", "bad_list", "clock", "future", "duplicate", "price", "volume", "nan_price"])
def test_page_contract_failures_cannot_complete_history(change):
    inp = args()
    page = raw_page(inp[3]["anchor_frame"].date)
    if change in {"market", "symbol", "type", "right"}:
        page[change] = "wrong"
    elif change == "missing_list":
        del page["list"]
    elif change == "bad_list":
        page["list"] = {"unexpected": 1}
    elif change == "clock":
        page["list"][0]["latestTime"] = "invalid"
    elif change == "future":
        page["list"][0]["latestTime"] = 20261012160000000
    elif change == "duplicate":
        page["list"].append(dict(page["list"][0]))
    elif change == "nan_price":
        page["list"][0]["open"] = "invalid"
    else:
        page["list"][0]["volume" if change == "volume" else "open"] = 8
    result = run(inp, page_source=Pages(page))
    assert result.coverage["reason"] == "contract_error"
    assert not result.coverage["compatible_with_anchor"] and not result.coverage["source_origin"]
    assert not result.coverage["can_resume"]


@pytest.mark.parametrize("mutation", ["cutoff", "basis", "frame", "state", "source"])
def test_resume_rejects_changed_evidence_and_cursor(mutation):
    inp = args("30m")
    first = run(inp, page_source=Pages(raw_page(inp[3]["anchor_frame"].date, "30m")), max_pages=1)
    pages = Pages(raw_page([], "30m"))
    if mutation == "cutoff":
        inp[0]["source_closed_at"] += 60
    elif mutation == "basis":
        inp[3]["anchor_frame"].attrs["price_basis_revision"] = "changed"
        inp[2]["price_basis_revision"] = "changed"
    elif mutation == "frame":
        first.frame.loc[0, "close"] += .1
    elif mutation == "state":
        first.state["cursor"] = "2000-01-01T00:00:00+00:00"
    else:
        pages.identity = "new-source"
    with pytest.raises(history.HistoryContractError):
        run(inp, page_source=pages, previous=first)
    assert not pages.calls


def test_frozen_long_history_reuses_exact_input_without_provider():
    inp = args(anchor=source_frame(pd.date_range("2026-08-01 20:00Z", "2026-10-09 20:00Z", freq="D")))
    result = run(inp, page_source=Pages())
    assert result.coverage["reason"] == "frozen_target"
    assert result.coverage["target_reached"] and not result.coverage["source_origin"]
    pd.testing.assert_frame_equal(result.frame, inp[3]["anchor_frame"])


def test_other_provider_never_calls_native_source_or_claims_origin(monkeypatch):
    inp = args("30m")
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "longbridge")
    result = run(inp, page_source=Pages())
    assert result.coverage["reason"] == "unsupported_source"
    assert not result.coverage["source_origin"] and not result.coverage["target_reached"]
    pd.testing.assert_frame_equal(result.frame, inp[3]["anchor_frame"])


def test_original_bad_geometry_is_preserved_and_never_promoted_to_valid():
    from chanlun.screening.rules import validate_frame
    inp = args()
    inp[3]["anchor_frame"].loc[0,"low"] = 10.1
    page = raw_page(inp[3]["anchor_frame"].date)
    page["list"][0]["low"] = 10.1
    result = run(inp, page_source=Pages(page), max_pages=1)
    assert result.frame.iloc[0]["low"] == 10.1 > result.frame.iloc[0]["open"]
    assert result.coverage["consumer_validation_required"]
    assert validate_frame(result.frame,{}) == ["INVALID_BARS"]


def test_origin_before_original_history_covered_cannot_claim_compatible():
    inp = args("30m")
    page = raw_page([inp[3]["anchor_frame"].date.max()], "30m")
    result = run(inp, page_source=Pages(page, raw_page([],"30m")))
    assert result.coverage["source_origin"]
    assert not result.coverage["compatible_with_anchor"]
    assert result.coverage["error_code"] == "origin_before_anchor_covered"


def test_missing_anchor_row_in_retrieved_range_rejects_gap():
    inp = args(anchor=source_frame(pd.date_range("2026-10-09 19:58Z",periods=3,freq="min")))
    result = run(inp, page_source=Pages(raw_page(inp[3]["anchor_frame"].date.iloc[[0,2]])))
    assert result.coverage["reason"] == "contract_error"
    assert result.coverage["error_code"] == "anchor_rows_missing"


@pytest.mark.parametrize("failure", [False,True])
def test_default_source_reuses_private_client_and_bounds_every_http(monkeypatch,failure):
    import requests
    from chanlun.exchange import exchange_usmart, usmart_rate_limit
    from chanlun.exchange.lb_priority import quote_request_priority
    seen = {"clients":0}
    monkeypatch.setattr(requests,'Session',lambda:SimpleNamespace(
        post=lambda *a,**k:seen.update(http=k),close=lambda:seen.update(closed=True)))
    class Limiter:
        def wait(self, **kwargs):
            seen["gate"] = kwargs
    class Client:
        def __init__(self, **kwargs):
            seen['clients'] += 1
            seen["client"] = kwargs
            self.gate = kwargs["rate_limiter"]
            self.session = kwargs['session']
        def quote(self, endpoint, payload, *, transient_retries):
            assert transient_retries == 0
            seen["request"] = (endpoint,payload,quote_request_priority())
            self.gate.wait(host="https://fixture.invalid",channel="public-fixture",endpoint=endpoint,
                           priority=quote_request_priority())
            self.session.post('https://fixture.invalid',timeout=999)
            if failure:
                raise RuntimeError("offline")
            return {"list":None}
    monkeypatch.setattr(usmart_rate_limit,"USmartRateLimiter",Limiter)
    monkeypatch.setattr(exchange_usmart,"USmartClient",Client)
    fetch = history.USmartPageSource(monotonic=lambda:10.)
    kwargs = dict(code="NVDA.US",frequency="5m",cursor=pd.Timestamp("2026-10-09 20:00Z"),right=1,remaining=4.)
    if failure:
        with pytest.raises(RuntimeError):fetch(**kwargs)
    else:
        assert fetch(**kwargs)=={"list":None}
    assert not seen.get('closed') and seen['http']['timeout'] == 2.
    assert seen['clients'] == 1
    assert seen["request"] == ("kline",{"secuId":"usNVDA","type":2,"start":20261009160000000,"right":1,"count":300},"background")
    assert seen["gate"]["timeout"] == 2. and seen["gate"]["priority"] == "background"
    fetch.close()
    assert seen['closed']


def test_page_and_slice_auth_reuses_login_and_recovers_one_401(monkeypatch):
    import requests
    from chanlun.exchange import exchange_usmart, usmart_rate_limit
    from tests.exchange.test_usmart_auth_refresh import reply
    for key,value in {'CHANNEL':'fixture','TOKEN':'','PHONE':'fixture-phone','LOGIN_PASSWORD':'fixture-password',
                      'PRIVATE_KEY':'fixture-private','PUBLIC_KEY':'fixture-public'}.items():
        monkeypatch.setattr(config,'USMART_'+key,value)
    replies = iter([reply(token='initial'),reply(),reply(401),reply(token='fresh'),reply(),reply()])
    calls = []
    def post(url,**kwargs):
        calls.append({'url':url,**kwargs})
        return next(replies)
    monkeypatch.setattr(requests,'Session',lambda:SimpleNamespace(post=post,close=lambda:None))
    monkeypatch.setattr(usmart_rate_limit,'USmartRateLimiter',lambda:SimpleNamespace(wait=lambda **_:None))
    monkeypatch.setattr(exchange_usmart.USmartClient,'_get_signer',lambda *a,**k:SimpleNamespace(
        sign=lambda *a,**k:'fixture-signature',encrypt_login_field=lambda _:'fixture-encrypted'))
    fetch = history.USmartPageSource()
    options = dict(code='NVDA.US',frequency='1m',cursor=pd.Timestamp('2026-10-09 20:00Z'),right=1,remaining=4.)
    for _ in range(3):
        assert fetch(**options)=={'value':1}
    assert [c['url'].rsplit('/',1)[-1] for c in calls]==['login','kline','kline','login','kline','kline']
    assert calls[-1]['headers']['Authorization']=='fresh'
    assert all(0<c['timeout']<=2 for c in calls)
    fetch.close()


def test_fetch_false_preserves_resumable_state_without_network():
    inp = args('30m')
    pages = Pages(raw_page(inp[3]['anchor_frame'].date,'30m'))
    initial = run(inp,page_source=pages,fetch=False)
    assert initial.coverage['can_resume'] and initial.coverage['pages']==0
    assert not pages.calls
    next_slice = run(inp,page_source=pages,previous=initial,max_pages=1)
    assert next_slice.coverage['pages']==1


def test_deadline_includes_401_login_and_retains_refresh_cooldown(monkeypatch):
    import requests
    from chanlun.exchange import exchange_usmart, usmart_rate_limit
    from tests.exchange.test_usmart_auth_refresh import reply
    for key,value in {'CHANNEL':'fixture','TOKEN':'expired','PHONE':'fixture-phone','LOGIN_PASSWORD':'fixture-password',
                      'PRIVATE_KEY':'fixture-private','PUBLIC_KEY':'fixture-public'}.items():
        monkeypatch.setattr(config,'USMART_'+key,value)
    now,calls=[0.],[]
    def post(url,**kwargs):
        calls.append(url)
        now[0] += 2
        return reply(401)
    monkeypatch.setattr(requests,'Session',lambda:SimpleNamespace(post=post,close=lambda:None))
    monkeypatch.setattr(usmart_rate_limit,'USmartRateLimiter',lambda:SimpleNamespace(wait=lambda **_:None))
    monkeypatch.setattr(exchange_usmart.USmartClient,'_get_signer',lambda *a,**k:SimpleNamespace(
        sign=lambda *a,**k:'fixture-signature',encrypt_login_field=lambda _:'fixture-encrypted'))
    source=history.USmartPageSource(monotonic=lambda:now[0])
    result=run(args(),page_source=source,monotonic=lambda:now[0],timeout_seconds=1.)
    assert result.coverage['reason']=='deadline' and result.coverage['can_resume']
    assert result.coverage['next_retry']>=30
    assert len(calls)==1 and calls[0].endswith('/kline')
    source.close()


def test_provider_retry_after_is_retained_for_queue_budget():
    from chanlun.exchange.errors import USmartAPIError
    result=run(args(),page_source=Pages(USmartAPIError('safe',status_code=503,retry_after=60)))
    assert result.coverage['reason']=='source_error' and result.coverage['next_retry']==60


def test_invalid_event_and_budget_inputs_do_not_call_provider():
    inp = args()
    pages = Pages()
    for overrides in ({"max_pages":0},{"max_pages":65},{"timeout_seconds":float("inf")},
                      {"timeout_seconds":0}):
        with pytest.raises(ValueError):run(inp,page_source=pages,**overrides)
    inp[3]["aligned_cutoff"] += 60
    with pytest.raises(history.HistoryContractError,match="cutoff_after_event"):
        run(inp,page_source=pages)
    assert not pages.calls


def test_real_recorded_old_pages_decode_using_original_provider_parser():
    import json
    root = Path(__file__).parents[1] / "fixtures" / "notification_history"
    for frequency, expected_rows in (("1m",300),("5m",300),("30m",0)):
        raw = json.loads((root/f"NVDA_US_{frequency}_before_first_response.json").read_text(encoding='utf8'))
        cursor = pd.Timestamp({"1m":"2026-09-30 11:26:59.999", "5m":"2026-08-17 13:04:59.999",
                               "30m":"2026-07-17 09:59:59.999"}[frequency],tz="US/Eastern")
        decoded = history._decode_page(raw,code="NVDA.US",frequency=frequency,cursor=cursor,right=1)
        if not expected_rows:
            assert decoded is None
        else:
            reference = ExchangeUSmart("us",client=SimpleNamespace(quote=lambda *_:raw))._fetch_native_klines(
                "NVDA.US",frequency,end_date=cursor,args={"pages":1,"_history_keep_overlap":True})
            reference.attrs.pop("_usmart_history_fetch")
            reference.attrs["bar_time_label"] = "end"
            pd.testing.assert_frame_equal(decoded,reference)
            assert len(decoded) == expected_rows and decoded.attrs == reference.attrs
