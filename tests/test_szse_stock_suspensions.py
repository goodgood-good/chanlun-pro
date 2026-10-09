"""Real SZSE report contracts and conservative whole-session pairing."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from chanlun.screening import szse_stock_suspensions as szse
from chanlun.screening.sessions import CN, _digest

FIXTURES = Path(__file__).parent / "fixtures" / "szse_stock_suspensions"


def response(name="002036_history"):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))["data"]


def records(name="002036_history"):
    return response(name)[0]["data"]


def clock(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=CN).timestamp())


@pytest.mark.parametrize(
    "code,fixture,began,resumed",
    [
        ("SZ.002036", "002036_history", "2025-12-18", "2025-12-25"),
        ("SZ.002047", "002047_history", "2025-10-20", "2025-10-27"),
    ],
)
def test_real_separate_stop_and_resume_rows_prove_closed_dates(
    code, fixture, began, resumed
):
    assert szse.intervals_from_records(
        code, records(fixture), clock(began), clock(resumed + "T15:00")
    ) == [[clock(began), clock(resumed) - 1]]


def test_real_000430_has_four_distinct_stops_despite_changed_security_names():
    result = szse.intervals_from_records(
        "SZ.000430",
        records("000430_full"),
        clock("2025-11-01"),
        clock("2026-10-09T15:00"),
    )
    assert result == [
        [clock(start), clock(end) - 1]
        for start, end in [
            ("2025-11-04", "2025-11-05"),
            ("2025-12-26", "2025-12-29"),
            ("2026-02-11", "2026-02-12"),
            ("2026-05-15", "2026-05-18"),
        ]
    ]


@pytest.mark.parametrize("fixture", ["002036_middle_day", "002036_resume_day"])
def test_real_date_filter_does_not_prove_an_unreported_start(fixture):
    assert (
        szse.intervals_from_records(
            "SZ.002036", records(fixture), clock("2025-12-19"), clock("2025-12-26")
        )
        == []
    )


def test_cross_window_keeps_the_disclosed_start_without_inventing_one():
    result = szse.intervals_from_records(
        "SZ.002036", records(), clock("2025-12-19"), clock("2025-12-26")
    )
    assert result == [[clock("2025-12-18"), clock("2025-12-25") - 1]]


@pytest.mark.parametrize(
    "rows",
    [
        [records()[1]],
        [{**records("000430_full")[-1], "tpsj": "半天"}],
        [{**records("000430_full")[-1], "tpkssj": "2025-11-04 09:30:00"}],
        [records()[1], {**records()[1], "tpkssj": "2025-12-22 13:00:00"}, records()[0]],
    ],
)
def test_unclosed_intraday_or_ambiguous_events_do_not_excuse_quotes(rows):
    code = "SZ." + rows[0]["zqdm"]
    assert (
        szse.intervals_from_records(
            code, rows, clock("2025-10-01"), clock("2026-01-01")
        )
        == []
    )


def test_announcement_of_tomorrows_resume_cannot_exempt_an_unclosed_interval():
    assert (
        szse.intervals_from_records(
            "SZ.002036", records(), clock("2025-12-18"), clock("2025-12-24T15:00")
        )
        == []
    )
    assert (
        szse.intervals_from_records(
            "SZ.002036", records(), clock("2025-12-18"), clock("2025-12-25T08:00")
        )
        == []
    )


@pytest.mark.parametrize("closed", [False, True])
def test_unknown_start_and_end_block_both_pairing_and_direct_closed_records(closed):
    base = {"zqdm": "002036", "zqjc": "联创电子", "tpyy": "重大事项"}
    unknown = {
        **base,
        "tpkssj": "2026-06-01 开市",
        "fpkssj": "2026-06-04 13:00:00",
        "tpsj": "未知",
    }
    rows = [
        unknown,
        {
            **base,
            "tpkssj": "2026-06-02 开市",
            "fpkssj": "2026-06-08 开市" if closed else "",
            "tpsj": "1天" if closed else "停牌",
        },
    ]
    if not closed:
        rows.append(
            {**base, "tpkssj": "", "fpkssj": "2026-06-08 开市", "tpsj": "取消停牌"}
        )
    assert (
        szse.intervals_from_records(
            "SZ.002036", rows, clock("2026-06-01"), clock("2026-06-09")
        )
        == []
    )


@pytest.mark.parametrize(
    "code,start,end",
    [
        ("SZ.159001", 1, 2),
        ("SH.002036", 1, 2),
        ("SZ.399001", 1, 2),
        ("SZ.002036", 3, 2),
        ("SZ.002036", 1.0, 2),
        ("SZ.002036", -1, 2),
    ],
)
def test_invalid_identity_or_window_never_makes_a_request(
    code, start, end, monkeypatch
):
    fetch = Mock()
    monkeypatch.setattr(szse, "_fetch", fetch)
    with pytest.raises(ValueError):
        szse.suspension_evidence(code, start, end)
    fetch.assert_not_called()


def session_for(monkeypatch, bodies):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.side_effect = [
        Mock(json=Mock(return_value=body), raise_for_status=Mock()) for body in bodies
    ]
    monkeypatch.setattr(szse.requests, "Session", Mock(return_value=session))
    return session


def test_fetch_uses_documented_symbol_and_date_filters_and_retains_source_pages(
    monkeypatch,
):
    body = response()
    session = session_for(monkeypatch, [body])
    actual = szse._fetch("SZ.002036", "2025-12-15", "2025-12-30")
    assert actual["records"] == records()
    assert actual["source_pages"][0]["response"] == body
    params = session.get.call_args.kwargs["params"]
    assert params["txtDmorjc"] == "002036"
    assert params["txtKsrq"] == "2025-12-15" and params["txtZzrq"] == "2025-12-30"
    assert params["CATALOGID"] == "1798" and params["TABKEY"] == "tab1"
    assert session.trust_env is False


def test_real_empty_page_is_complete_not_a_phantom_halt(monkeypatch):
    session_for(monkeypatch, [response("002036_middle_day")])
    assert szse._fetch("SZ.002036", "2025-12-19", "2025-12-19")["records"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body[0]["metadata"].update(catalogid="other"),
        lambda body: body[0]["metadata"].update(pageno=2),
        lambda body: body[0]["metadata"].update(recordcount=3),
        lambda body: body[0]["metadata"].update(recordcount=201, pagecount=21),
        lambda body: body[0].update(error="source error"),
        lambda body: body[0]["data"][0].update(zqdm="002047"),
        lambda body: body[0]["data"][1].update(tpkssj="2024-12-18 开市"),
        lambda body: body[0]["data"][1].update(tpkssj="2025-99-18 开市"),
    ],
)
def test_bad_source_contract_cannot_publish_partial_evidence(monkeypatch, mutate):
    body = response()
    mutate(body)
    session_for(monkeypatch, [body])
    with pytest.raises(ValueError):
        szse._fetch("SZ.002036", "2025-12-15", "2025-12-30")


@pytest.mark.parametrize(
    "fault", ["duplicate", "total_changed", "wrong_page", "missing_row"]
)
def test_pagination_repeats_and_moving_totals_are_rejected(monkeypatch, fault):
    first = response()
    second = deepcopy(first)
    first[0]["metadata"].update(pagesize=1, recordcount=2, pagecount=2)
    first[0]["data"] = first[0]["data"][:1]
    second[0]["metadata"].update(pagesize=1, recordcount=2, pagecount=2, pageno=2)
    second[0]["data"] = (
        second[0]["data"][:1] if fault == "duplicate" else second[0]["data"][1:]
    )
    if fault == "total_changed":
        second[0]["metadata"].update(recordcount=3, pagecount=3)
    if fault == "wrong_page":
        second[0]["metadata"].update(pageno=1)
    if fault == "missing_row":
        second[0]["data"] = []
    session_for(monkeypatch, [first, second])
    with pytest.raises(ValueError):
        szse._fetch("SZ.002036", "2025-12-15", "2025-12-30")


@pytest.fixture
def cached_request(monkeypatch):
    start, cutoff = clock("2025-12-19"), clock("2025-12-26T15:00")
    fetched = {
        "records": records(),
        "source_pages": [
            {
                "params": szse._params("SZ.002036", "2024-12-19", "2025-12-26", 1),
                "response": response(),
            }
        ],
    }
    fetch = Mock(return_value=fetched)
    monkeypatch.setattr(szse, "_fetch", fetch)
    monkeypatch.setattr(szse.time, "time", lambda: cutoff + 100)
    return start, cutoff, fetch


def test_cache_lookback_recovers_a_start_before_input_and_preserves_provenance(
    tmp_path, cached_request
):
    start, cutoff, fetch = cached_request
    result = szse.suspension_evidence("SZ.002036", start, cutoff, cache_root=tmp_path)
    assert result["intervals"] == [[clock("2025-12-18"), clock("2025-12-25") - 1]]
    fetch.assert_called_once_with("SZ.002036", "2024-12-19", "2025-12-26")
    assert result["records"] == records()
    saved = json.loads(
        (tmp_path / "szse_stock_suspensions_SZ.002036.json").read_text(encoding="utf-8")
    )
    assert saved["source_pages"] and saved["source_url"] == szse.SOURCE_URL
    assert (
        szse.suspension_evidence("SZ.002036", start, cutoff, cache_root=tmp_path)
        == result
    )
    assert fetch.call_count == 1


@pytest.mark.parametrize(
    "fault",
    [
        "symbol",
        "query_from",
        "query_through",
        "record_identity",
        "hash",
        "catalog_id",
        "source_page",
        "source_error",
        "query_parameters",
        "source_records",
    ],
)
def test_incompatible_or_corrupt_cache_is_refetched(tmp_path, cached_request, fault):
    start, cutoff, fetch = cached_request
    szse.suspension_evidence("SZ.002036", start, cutoff, cache_root=tmp_path)
    path = tmp_path / "szse_stock_suspensions_SZ.002036.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("sha256")
    if fault == "symbol":
        data["symbol"] = "SZ.002047"
    if fault == "query_from":
        data["query_from"] = "2025-12-20"
    if fault == "query_through":
        data["query_through"] = "2025-12-24"
    if fault == "record_identity":
        data["records"][0]["zqdm"] = "002047"
    if fault == "catalog_id":
        data["catalog_id"] = "OTHER"
    if fault == "source_page":
        data["source_page"] = "https://www.szse.cn/other/"
    if fault == "source_error":
        data["source_pages"][0]["response"][0]["error"] = "query failed"
    if fault == "query_parameters":
        data["source_pages"][0]["params"]["txtDmorjc"] = "002047"
    if fault == "source_records":
        data["source_pages"][0]["response"][0]["data"][0]["tpyy"] = "different record"
    path.write_text(
        json.dumps({**data, "sha256": "bad" if fault == "hash" else _digest(data)})
    )
    assert szse.suspension_evidence("SZ.002036", start, cutoff, cache_root=tmp_path)[
        "intervals"
    ]
    assert fetch.call_count == 2


def test_orphan_resume_in_a_fresh_cache_never_invents_the_requested_start(
    tmp_path, cached_request
):
    start, cutoff, fetch = cached_request
    fetch.return_value = {
        "records": records("002036_resume_day"),
        "source_pages": [
            {
                "params": szse._params("SZ.002036", "2024-12-19", "2025-12-26", 1),
                "response": response("002036_resume_day"),
            }
        ],
    }
    assert (
        szse.suspension_evidence("SZ.002036", start, cutoff, cache_root=tmp_path)[
            "intervals"
        ]
        == []
    )


def test_cache_failure_does_not_discard_verified_fetched_evidence(
    tmp_path, cached_request, monkeypatch
):
    start, cutoff, _ = cached_request
    monkeypatch.setattr(
        szse.os, "replace", Mock(side_effect=PermissionError("optional cache"))
    )
    assert szse.suspension_evidence("SZ.002036", start, cutoff, cache_root=tmp_path)[
        "intervals"
    ]


def test_expired_cache_is_not_used_when_source_is_unavailable(tmp_path, cached_request):
    start, cutoff, fetch = cached_request
    szse.suspension_evidence("SZ.002036", start, cutoff, cache_root=tmp_path)
    path = tmp_path / "szse_stock_suspensions_SZ.002036.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("sha256")
    data["fetched_at"] = cutoff - 7200
    path.write_text(json.dumps({**data, "sha256": _digest(data)}))
    fetch.side_effect = requests.ConnectionError("isolated official endpoint failure")
    with pytest.raises(requests.ConnectionError):
        szse.suspension_evidence("SZ.002036", start, cutoff, cache_root=tmp_path)
