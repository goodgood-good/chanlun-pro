"""W2 测试: /symbols/list 的 ETag + 304 + market filter 缓存行为.

验证:
1. 首次请求返回 200 + ETag 头 + Cache-Control 头
2. 二次请求带相同 If-None-Match → 304 (无 body)
3. 同 market 不同 q → 不同 ETag
4. 同 market 不同 page/page_size → 不同 ETag
5. ?all=1 vs 分页 → 不同 ETag
6. _market_filtered_cache 命中: TTL 内不重跑 _apply_market_filter
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin


class _Anon(UserMixin):
    id = "test"


_FAKE_A_STOCKS = [
    {"code": f"SH.{600000 + i}", "name": f"标的{i}", "type": "stock_cn",
     "code_lower": f"sh.{600000 + i}", "name_lower": f"标的{i}",
     "pinyin_initials": f"bd{i}"}
    for i in range(50)
]


@pytest.fixture
def app(monkeypatch):
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.config["LOGIN_DISABLED"] = True
    flask_app.secret_key = "w2-test"

    lm = LoginManager()
    lm.init_app(flask_app)
    lm.user_loader(lambda _id: _Anon())

    # mock 标的来源
    monkeypatch.setattr(
        "cl_app.services.stock_list.get_cached_processed_stocks",
        lambda market, allow_sync_fallback=True: list(_FAKE_A_STOCKS),
    )
    monkeypatch.setattr(
        "cl_app.blueprints.symbols.get_cached_processed_stocks",
        lambda market, allow_sync_fallback=True: list(_FAKE_A_STOCKS),
    )

    # 每个用例独立 cache, 避免上一用例污染
    from cl_app.blueprints.symbols import _market_filtered_cache
    _market_filtered_cache.clear()

    from cl_app.blueprints.symbols import symbols_bp
    flask_app.register_blueprint(symbols_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def test_first_request_returns_200_with_etag_and_cache_control(client):
    resp = client.get("/symbols/list", query_string={"market": "a"})
    assert resp.status_code == 200
    assert "ETag" in resp.headers
    assert resp.headers["ETag"].startswith('W/"')
    assert "Cache-Control" in resp.headers
    assert "max-age=600" in resp.headers["Cache-Control"]
    data = resp.get_json()
    assert data["ok"] is True
    assert data["total"] == 50


def test_if_none_match_returns_304_no_body(client):
    resp1 = client.get("/symbols/list", query_string={"market": "a"})
    assert resp1.status_code == 200
    etag = resp1.headers["ETag"]

    resp2 = client.get(
        "/symbols/list",
        query_string={"market": "a"},
        headers={"If-None-Match": etag},
    )
    assert resp2.status_code == 304
    # 304 应无 body
    assert resp2.data == b""


def test_different_query_yields_different_etag(client):
    resp1 = client.get("/symbols/list", query_string={"market": "a", "q": "600001"})
    resp2 = client.get("/symbols/list", query_string={"market": "a", "q": "600002"})
    assert resp1.headers["ETag"] != resp2.headers["ETag"]


def test_different_page_yields_different_etag(client):
    resp1 = client.get("/symbols/list", query_string={"market": "a", "page": "1"})
    resp2 = client.get("/symbols/list", query_string={"market": "a", "page": "2"})
    assert resp1.headers["ETag"] != resp2.headers["ETag"]


def test_all_vs_paged_yields_different_etag(client):
    resp_all = client.get("/symbols/list", query_string={"market": "a", "all": "1"})
    resp_pg = client.get("/symbols/list", query_string={"market": "a"})
    assert resp_all.headers["ETag"] != resp_pg.headers["ETag"]
    # 不同 etag 但都 200 (无缓存命中)
    assert resp_all.status_code == 200 and resp_pg.status_code == 200


def test_market_filter_cache_skips_repeated_filter(client, monkeypatch):
    """同一 market 连续 N 次请求, _apply_market_filter 应仅在第一次跑 (TTL 内)."""
    call_counter = {"n": 0}

    from cl_app.blueprints.symbols import _apply_market_filter as orig_filter

    def counting_filter(market, all_stocks, for_prewarm=False):
        call_counter["n"] += 1
        return orig_filter(market, all_stocks, for_prewarm)

    monkeypatch.setattr(
        "cl_app.blueprints.symbols._apply_market_filter", counting_filter
    )

    # 清缓存确保第一次走 miss
    from cl_app.blueprints.symbols import _market_filtered_cache
    _market_filtered_cache.clear()

    # 3 次请求 (不同 q, 不同 page), 但 market 相同
    client.get("/symbols/list", query_string={"market": "a"})
    client.get("/symbols/list", query_string={"market": "a", "q": "600001"})
    client.get("/symbols/list", query_string={"market": "a", "page": "2"})

    # _apply_market_filter 应只被调一次 (后两次命中 TTL 缓存)
    assert call_counter["n"] == 1, (
        f"_apply_market_filter 调用 {call_counter['n']} 次, 缓存未生效"
    )


def test_304_short_circuits_query_filter(client):
    """ETag 命中时, 即使 q 不同也应该 304 (因 q 影响 ETag) — 反向验证: 同 q 命中."""
    resp1 = client.get("/symbols/list", query_string={"market": "a", "q": "600001"})
    etag = resp1.headers["ETag"]

    # 同样 (market, q): 期望 304
    resp2 = client.get(
        "/symbols/list",
        query_string={"market": "a", "q": "600001"},
        headers={"If-None-Match": etag},
    )
    assert resp2.status_code == 304
