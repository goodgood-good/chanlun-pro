"""A manual exact search must work outside the small startup catalog."""
from cl_app import create_app
from cl_app.blueprints import tv


def test_explicit_stock_search_resolves_one_identity_without_enumerating_market(monkeypatch):
    app = create_app(test_config={"TESTING": True, "LOGIN_DISABLED": True,
                                 "VALIDATE_WEB_SECURITY": False})
    monkeypatch.setattr(tv, "get_cached_processed_stocks", lambda *args, **kwargs: [])
    provider = object()
    calls = []
    monkeypatch.setattr(tv, "get_exchange", lambda market: provider)

    def resolve(exchange, code, *, allow_code_fallback):
        assert exchange is provider and allow_code_fallback is False
        calls.append(code)
        return {"code": code, "name": "test stock"}

    monkeypatch.setattr(tv, "resolve_bounded_stock_info", resolve)
    response = app.test_client().get("/tv/search?exchange=a&query=600088")
    assert response.status_code == 200
    assert [row["symbol"] for row in response.json] == ["SH.600088"]
    assert calls == ["SH.600088"]
    assert app.test_client().get("/tv/search?exchange=a&query=600").json == []
    assert calls == ["SH.600088"]
