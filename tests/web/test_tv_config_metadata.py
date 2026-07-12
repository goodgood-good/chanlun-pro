"""Regression tests for metadata-free TradingView configuration."""

from cl_app.blueprints import tv as tv_module


class _ForbiddenMetadataAccess:
    def __getitem__(self, key):
        raise AssertionError(f"/tv/config accessed market metadata: {key}")


def test_tv_config_does_not_load_market_metadata(monkeypatch):
    monkeypatch.setattr(
        tv_module,
        "market_frequencys",
        _ForbiddenMetadataAccess(),
    )

    payload = tv_module.tv_config.__wrapped__()

    assert payload["supported_resolutions"] == list(
        tv_module.frequency_maps.values()
    )
    assert payload["supports_search"] is True
    assert payload["supports_group_request"] is False
