"""
统一的密钥/凭证安全工具：

- ``get_flask_secret_key()``：返回 Flask 会话密钥；按 env > config > 持久化文件 顺序解析。
- ``get_fernet()`` / ``encrypt_str`` / ``decrypt_str``：基于 Flask 密钥派生的对称加密，
  用于把飞书 app_secret 等敏感配置加密落库。
- ``mask_secret()``：用于前端回显时遮蔽中间字符。
- ``verify_login_password()``：只校验 Werkzeug ``pbkdf2:``/``scrypt:`` 哈希。

约束：
- 不抛出运行时致命异常；密钥派生失败时退化为可读的 ValueError。
- 无当前密文前缀的 secret 一律失效关闭，不进入生产调用。
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import time
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet, InvalidToken

from chanlun import config


_FERNET_SALT = b"chanlun_pro_fs_keys"  # 固定 salt：保证同一 SECRET_KEY 派生出稳定的 Fernet 密钥
_SECRET_FILE_NAME = ".flask_secret_key"
_PASSWORD_HASH_PREFIXES = ("pbkdf2:", "scrypt:")
_RUNTIME_CREDENTIALS_SCHEMA = "chanlun-runtime-credentials"
_RUNTIME_CREDENTIALS_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "runtime_credentials.local.json"
)


def get_login_password() -> str:
    """Return the configured Web password with environment-first precedence."""
    return os.environ.get("CHANLUN_LOGIN_PWD") or str(
        getattr(config, "LOGIN_PWD", "") or ""
    )


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


def validate_web_security_config(host: str, password: str) -> None:
    """Require a hashed login secret; external listeners also require HTTPS."""
    configured_password = str(password or "").strip()
    if not configured_password:
        raise ValueError(
            "WEB_HOST requires CHANLUN_LOGIN_PWD/LOGIN_PWD"
        )
    if not configured_password.startswith(_PASSWORD_HASH_PREFIXES):
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


def get_fernet() -> Fernet:
    """从 Flask SECRET_KEY 派生 Fernet 实例（PBKDF2-HMAC-SHA256，固定 salt）。"""
    secret = get_flask_secret_key().encode("utf-8")
    derived = hashlib.pbkdf2_hmac("sha256", secret, _FERNET_SALT, 100_000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(derived))


_ENC_PREFIX = "enc::fernet::"


def encrypt_str(plaintext: Optional[str]) -> str:
    """加密字符串；空值原样返回空字符串。带版本前缀以便日后轮换。"""
    if not plaintext:
        return ""
    token = get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt_str(value: Optional[str]) -> str:
    """
    解密字符串：
    - 空值返回空字符串
    - 带 ``enc::fernet::`` 前缀按密文解密
    - 无当前密文前缀时返回空值，禁止明文 secret 进入生产调用
    """
    if not value:
        return ""
    if not value.startswith(_ENC_PREFIX):
        return ""
    token = value[len(_ENC_PREFIX):]
    try:
        return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""  # 密钥变更等导致解密失败：返回空，调用方将走默认配置


def _is_valid_dingtalk_webhook(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "oapi.dingtalk.com"
        and parsed.path == "/robot/send"
        and parse_qs(parsed.query).get("access_token")
    )


def get_dingtalk_webhook(
    credentials_path: os.PathLike[str] | str | None = None,
) -> str:
    """Resolve the sole DingTalk webhook from environment or local config.

    ``CHANLUN_DINGTALK_WEBHOOK`` has explicit precedence. Missing or malformed
    local credentials fail closed.  The default file is deliberately ignored by
    Git; ``CHANLUN_RUNTIME_CREDENTIALS_PATH`` may point outside the repository.
    """

    configured = os.environ.get("CHANLUN_DINGTALK_WEBHOOK")
    if configured is not None:
        normalized = configured.strip()
        return normalized if _is_valid_dingtalk_webhook(normalized) else ""

    configured_path = os.environ.get("CHANLUN_RUNTIME_CREDENTIALS_PATH")
    path = Path(credentials_path) if credentials_path is not None else (
        Path(configured_path).expanduser()
        if configured_path and configured_path.strip()
        else _RUNTIME_CREDENTIALS_PATH
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != _RUNTIME_CREDENTIALS_SCHEMA
        or not isinstance(payload.get("dingtalk_webhook"), str)
    ):
        return ""
    webhook = payload["dingtalk_webhook"].strip()
    return webhook if _is_valid_dingtalk_webhook(webhook) else ""


def get_dingtalk_keyword(
    credentials_path: os.PathLike[str] | str | None = None,
) -> str:
    """Resolve the robot's required custom keyword from the same contract."""

    configured = os.environ.get("CHANLUN_DINGTALK_KEYWORD")
    if configured is not None:
        return configured.strip()
    configured_path = os.environ.get("CHANLUN_RUNTIME_CREDENTIALS_PATH")
    path = Path(credentials_path) if credentials_path is not None else (
        Path(configured_path).expanduser()
        if configured_path and configured_path.strip()
        else _RUNTIME_CREDENTIALS_PATH
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != _RUNTIME_CREDENTIALS_SCHEMA
        or not isinstance(payload.get("dingtalk_keyword"), str)
    ):
        return ""
    return payload["dingtalk_keyword"].strip()


def mask_secret(value: Optional[str], head: int = 2, tail: int = 2) -> str:
    """前端回显遮蔽：保留首尾少量字符，中间用 ``*`` 代替；过短则全部遮蔽。"""
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}{'*' * (len(value) - head - tail)}{value[-tail:]}"


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
