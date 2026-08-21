from cl_app.services.constants import (
    _MARKET_FREQUENCY_FALLBACKS,
    frequency_maps,
    resolution_maps,
)


def test_binance_hour_day_and_month_resolutions_are_round_trippable():
    expected = {
        "120m": "120",
        "3h": "180",
        "4h": "240",
        "6h": "360",
        "8h": "480",
        "12h": "720",
        "3d": "3D",
        "m": "1M",
    }

    for frequency, resolution in expected.items():
        assert frequency_maps[frequency] == resolution
        assert resolution_maps[resolution] == frequency


def test_binance_metadata_fallbacks_expose_all_supported_periods():
    required = {
        "1m",
        "2m",
        "3m",
        "5m",
        "10m",
        "15m",
        "30m",
        "60m",
        "120m",
        "3h",
        "4h",
        "6h",
        "8h",
        "12h",
        "d",
        "3d",
        "w",
        "m",
    }

    assert set(_MARKET_FREQUENCY_FALLBACKS["currency"]) == required
    assert set(_MARKET_FREQUENCY_FALLBACKS["currency_spot"]) == required
