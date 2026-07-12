from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from chanlun import security


def test_login_password_prefers_environment(monkeypatch):
    monkeypatch.setattr(security.config, "LOGIN_PWD", "config-value", raising=False)
    monkeypatch.setenv("CHANLUN_LOGIN_PWD", "env-value")

    assert security.get_login_password() == "env-value"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::"])
def test_external_bind_rejects_missing_password(host, monkeypatch):
    monkeypatch.setenv("CHANLUN_HTTPS", "1")

    with pytest.raises(ValueError, match="LOGIN_PWD"):
        security.validate_web_security_config(host, "")


def test_external_bind_rejects_plaintext_password(monkeypatch):
    monkeypatch.setenv("CHANLUN_HTTPS", "1")

    with pytest.raises(ValueError, match="hash"):
        security.validate_web_security_config("0.0.0.0", "plain-text")


def test_external_bind_rejects_hash_without_https(monkeypatch):
    monkeypatch.setenv("CHANLUN_HTTPS", "0")

    with pytest.raises(ValueError, match="HTTPS"):
        security.validate_web_security_config(
            "0.0.0.0", "scrypt:32768:8:1$stub$stub"
        )


def test_loopback_allows_passwordless_development(monkeypatch):
    monkeypatch.setenv("CHANLUN_HTTPS", "0")
    security.validate_web_security_config("127.0.0.1", "")


def test_external_bind_accepts_password_hash_with_https(monkeypatch):
    monkeypatch.setenv("CHANLUN_HTTPS", "1")
    security.validate_web_security_config(
        "0.0.0.0", "scrypt:32768:8:1$stub$stub"
    )


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_https_mode_accepts_explicit_truthy_values(monkeypatch, value):
    monkeypatch.setenv("CHANLUN_HTTPS", value)
    assert security.is_https_enabled() is True


def test_persisted_flask_secret_is_created_once_under_concurrency(
    monkeypatch, tmp_path
):
    path = tmp_path / ".flask_secret_key"
    entered = threading.Event()
    release = threading.Event()
    generated = []

    def generate_secret(_size):
        value = len(generated) + 1
        generated.append(value)
        entered.set()
        release.wait(timeout=5)
        return f"{value:064x}"

    monkeypatch.delenv("CHANLUN_FLASK_SECRET_KEY", raising=False)
    monkeypatch.setattr(security.config, "FLASK_SECRET_KEY", "", raising=False)
    monkeypatch.setattr(security, "_persisted_secret_path", lambda: path)
    monkeypatch.setattr(security.secrets, "token_hex", generate_secret)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(security.get_flask_secret_key)
        assert entered.wait(timeout=1)
        second = executor.submit(security.get_flask_secret_key)
        time.sleep(0.05)
        release.set()
        values = [first.result(timeout=2), second.result(timeout=2)]

    assert generated == [1]
    assert len(set(values)) == 1
    assert path.read_text(encoding="utf-8").strip() == values[0]


@pytest.mark.parametrize("contents", ["", "partial", "f" * 63])
def test_invalid_persisted_flask_secret_is_repaired_atomically(
    monkeypatch, tmp_path, contents
):
    path = tmp_path / ".flask_secret_key"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.delenv("CHANLUN_FLASK_SECRET_KEY", raising=False)
    monkeypatch.setattr(security.config, "FLASK_SECRET_KEY", "", raising=False)
    monkeypatch.setattr(security, "_persisted_secret_path", lambda: path)

    secret = security.get_flask_secret_key()

    assert len(secret) == 64
    assert all(char in "0123456789abcdef" for char in secret)
    assert path.read_text(encoding="utf-8") == secret
