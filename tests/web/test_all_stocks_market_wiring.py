"""标的查询与自选导入不得隐式展开全市场。"""
import pathlib



class _FakeEx:
    def __init__(self):
        self.called = False

    def all_stocks(self):
        self.called = True
        return [{"code": "HK.00700", "name": "TX"}]

    @staticmethod
    def support_frequencys():
        return {"5m": "5分钟"}


def test_zixuan_import_endpoint_uses_bounded_identity_wiring():
    src = pathlib.Path(
        "web/chanlun_chart/cl_app/blueprints/zixuan.py"
    ).read_text(encoding="utf-8")
    assert "_safe_all_stocks" not in src
    assert "_MAX_BOUNDED_IMPORT_SYMBOLS = 20" in src
    assert "admit_explicit_validation_codes(" in src
    assert "resolve_bounded_stock_info(" in src
    assert "ex.stock_info(code)" not in src
