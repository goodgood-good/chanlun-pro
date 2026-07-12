import pytest

import app as desktop_app


def test_web_port_uses_valid_environment_override(monkeypatch):
    monkeypatch.setenv("CHANLUN_WEB_PORT", "9915")

    assert desktop_app._get_web_port() == 9915


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_web_port_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("CHANLUN_WEB_PORT", value)

    with pytest.raises(ValueError, match="CHANLUN_WEB_PORT"):
        desktop_app._get_web_port()