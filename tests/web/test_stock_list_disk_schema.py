"""symbols 磁盘缓存必须保留 A 股市场过滤依赖的原始 type 字段。"""

import json

from cl_app.services import stock_list


def test_disk_cache_preserves_stock_type_under_current_schema(tmp_path, monkeypatch):
    cache_file = tmp_path / "a_stocks.json"
    monkeypatch.setattr(stock_list, "_stocks_cache_file", lambda _market: str(cache_file))

    stock_list._save_stocks_to_disk(
        "a",
        [
            {"code": "SH.510300", "name": "沪深300ETF", "type": "etf_cn"},
            {"code": "SH.600000", "name": "浦发银行", "type": "stock_cn"},
        ],
    )

    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload["schema"] == "chanlun-stock-list-cache"
    assert [stock["type"] for stock in payload["stocks"]] == ["etf_cn", "stock_cn"]
