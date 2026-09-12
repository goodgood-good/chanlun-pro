"""
Web 登录与会话密钥工具：

- ``get_flask_secret_key()``：返回 Flask 会话密钥；按 env > config > 持久化文件 顺序解析。
- ``verify_login_password()``：只校验 Werkzeug ``pbkdf2:``/``scrypt:`` 哈希。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import json
import os
import secrets
import time
from typing import Mapping
import unicodedata

from chanlun import config


_SECRET_FILE_NAME = ".flask_secret_key"
_PASSWORD_HASH_PREFIXES = ("pbkdf2:", "scrypt:")


def normalize_login_username(value: object) -> str:
    """Return the canonical, case-insensitive Web account name.

    Usernames are intentionally kept human-readable at the login boundary while
    control characters and ambiguous surrounding whitespace are rejected by
    returning an empty value.  NFKC makes full-width names behave consistently
    across browsers and operating systems.
    """

    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > 64:
        return ""
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        return ""
    return normalized.casefold()


@dataclass(frozen=True)
class WebLoginAccount:
    """Validated login account configuration."""

    username: str
    password_hash: str


def _configured_login_users_value():
    if "CHANLUN_LOGIN_USERS" in os.environ:
        return os.environ.get("CHANLUN_LOGIN_USERS")
    return getattr(config, "LOGIN_USERS", None)


def _parse_login_users(value: object) -> tuple[WebLoginAccount, ...]:
    """Parse a username-to-hash mapping and fail closed on invalid input."""

    if value is None or value == "":
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return ()
    if not isinstance(value, Mapping) or not value:
        return ()

    accounts: list[WebLoginAccount] = []
    seen: set[str] = set()
    for raw_username, raw_password_hash in value.items():
        username = normalize_login_username(raw_username)
        password_hash = (
            raw_password_hash.strip()
            if isinstance(raw_password_hash, str)
            else ""
        )
        if (
            not username
            or username in seen
            or not password_hash.startswith(_PASSWORD_HASH_PREFIXES)
        ):
            return ()
        seen.add(username)
        accounts.append(WebLoginAccount(username, password_hash))
    return tuple(accounts)


def get_login_accounts() -> tuple[WebLoginAccount, ...]:
    """Return the configured named Web accounts.

    ``CHANLUN_LOGIN_USERS`` takes precedence over ``config.LOGIN_USERS`` and
    must be a JSON object such as
    ``{"alice": "scrypt:...", "bob": "scrypt:..."}``.
    """

    return _parse_login_users(_configured_login_users_value())


def get_web_host() -> str:
    """Resolve the listener host with environment-first precedence."""
    return os.environ.get("CHANLUN_WEB_HOST") or str(
        getattr(config, "WEB_HOST", "127.0.0.1") or "127.0.0.1"
    )


def is_https_enabled() -> bool:
    """Return whether HTTPS reverse-proxy mode was explicitly enabled."""
    return os.environ.get("CHANLUN_HTTPS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_web_security_config(
    host: str,
    accounts: tuple[WebLoginAccount, ...],
) -> None:
    """Require hashed login credentials; external listeners also require HTTPS."""

    configured_passwords = [account.password_hash for account in accounts]
    if not configured_passwords or any(not value for value in configured_passwords):
        raise ValueError("WEB_HOST requires CHANLUN_LOGIN_USERS/LOGIN_USERS")
    if any(
        not configured_password.startswith(_PASSWORD_HASH_PREFIXES)
        for configured_password in configured_passwords
    ):
        raise ValueError(
            "WEB_HOST requires a pbkdf2:/scrypt: password hash"
        )
    if not _is_loopback_host(host) and not is_https_enabled():
        raise ValueError(
            "Non-loopback WEB_HOST requires HTTPS proxy mode (CHANLUN_HTTPS=1)"
        )

def _persisted_secret_path():
    """密钥持久化文件路径；放在用户数据目录下，避免和源码混在一起。"""
    return config.get_data_path() / _SECRET_FILE_NAME


def _is_valid_persisted_secret(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


@contextmanager
def _secret_file_lock(path, timeout=10.0):
    """Use an OS lock so process crashes cannot leave a stale logical lock."""
    lock_path = path.with_name(f"{path.name}.lock")
    stream = open(lock_path, "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + max(0.1, float(timeout))
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("timed out acquiring Flask secret lock")
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def get_flask_secret_key() -> str:
    """
    解析顺序：
    1. 环境变量 ``CHANLUN_FLASK_SECRET_KEY``
    2. ``config.FLASK_SECRET_KEY``（用户在 config.py 显式配置）
    3. 数据目录下 ``.flask_secret_key`` 文件（首次运行随机生成 32 字节并写入）
    """
    env_key = os.environ.get("CHANLUN_FLASK_SECRET_KEY")
    if env_key:
        return env_key

    cfg_key = getattr(config, "FLASK_SECRET_KEY", "") or ""
    if cfg_key:
        return cfg_key

    path = _persisted_secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _secret_file_lock(path):
        try:
            persisted_key = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, UnicodeDecodeError):
            persisted_key = ""
        if _is_valid_persisted_secret(persisted_key):
            return persisted_key

        key = secrets.token_hex(32)
        temp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            descriptor = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(
                descriptor, "w", encoding="utf-8", newline=""
            ) as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            try:
                os.chmod(path, 0o600)
            except (OSError, NotImplementedError):
                pass
            return key
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def verify_login_password(submitted: str, expected: str) -> bool:
    """
    登录密码验证：
    ``expected`` 必须是 Werkzeug ``pbkdf2:`` / ``scrypt:`` 哈希；其余格式
    直接拒绝。
    """
    if submitted is None or expected is None:
        return False
    if not expected.startswith(_PASSWORD_HASH_PREFIXES):
        return False
    from werkzeug.security import check_password_hash

    try:
        return check_password_hash(expected, submitted)
    except (ValueError, TypeError):
        return False
