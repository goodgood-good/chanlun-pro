"""Private, reloadable DingTalk destinations, separate from monitor state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qsl, urlsplit

from chanlun.persistence.file_lock import interprocess_file_lock


CHANNELS = {"holdings": "我的持仓", "others": "其他通知"}
FIELDS = {"webhook", "secret", "keyword"}


def webhook_token(value):
    try:
        url = urlsplit(value)
        tokens = [v for k, v in parse_qsl(url.query) if k == "access_token"]
        if (url.scheme == "https" and url.hostname == "oapi.dingtalk.com"
                and url.path == "/robot/send" and url.port in (None, 443)
                and not url.username and not url.password and not url.fragment
                and len(tokens) == 1 and tokens[0]
                and not any(c.isspace() for c in value)):
            return tokens[0]
    except (ValueError, TypeError):
        pass
    return ""


def event_channel(event):
    return ("holdings" if event.get("holding") is True
            or event.get("signal_type") in {"holding_risk", "consolidation_divergence"} else "others")


class DingTalkChannels:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None

    def _overrides(self):
        if self.path is None:
            return {}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise OSError("通知地址配置暂不可读取") from None
        if (not isinstance(saved, dict) or set(saved) - CHANNELS.keys()
                or any(not isinstance(values, dict) or set(values) - FIELDS
                       or any(not isinstance(v, str) for v in values.values()) for values in saved.values())):
            raise OSError("通知地址配置格式异常")
        return saved

    def read(self):
        saved = self._overrides()
        result = {}
        for channel in CHANNELS:
            prefix = "CHANLUN_DINGTALK_HOLDINGS_" if channel == "holdings" else "CHANLUN_DINGTALK_"
            result[channel] = {**{field: os.environ.get(prefix + field.upper(), "").strip() for field in FIELDS},
                               **saved.get(channel, {})}
        return result

    @staticmethod
    def _public(values):
        same = (webhook_token(values['holdings']['webhook'])
                and webhook_token(values['holdings']['webhook']) == webhook_token(values['others']['webhook']))
        result = {}
        for channel, credentials in values.items():
            token = webhook_token(credentials['webhook'])
            result[channel] = {
                "configured": bool(token) and not bool(same),
                "webhook_masked": ("https://oapi.dingtalk.com/robot/send?access_token=••••"
                                   + (token[-4:] if len(token) > 8 else "")) if token else "",
                "secret_configured": bool(credentials['secret']),
                "keyword": credentials['keyword'],
                "configuration_error": ("持仓与其他通知必须使用不同的机器人地址" if same
                    else "" if token else f"尚未配置{CHANNELS[channel]}的有效钉钉地址"),
            }
        return result

    def snapshot(self):
        return self._public(self.read())

    def credentials(self, channel):
        values = self.read()
        status = self._public(values)[channel]
        if not status['configured']:
            raise ValueError(status['configuration_error'])
        return values[channel]

    def save(self, body):
        if (not isinstance(body, dict) or not body or set(body) - CHANNELS.keys()
                or any(not isinstance(v, dict) or not v or set(v) - FIELDS for v in body.values())):
            raise ValueError("请按我的持仓、其他通知分别填写通知地址设置")
        patch = {}
        for channel, fields in body.items():
            patch[channel] = {}
            for field, value in fields.items():
                if not isinstance(value, str) or len(value) > (80 if field == 'keyword' else 2048):
                    raise ValueError("通知地址、签名或关键词格式不合法")
                value = value.strip()
                if any(ord(c) < 32 for c in value):
                    raise ValueError("通知地址设置不能包含控制字符")
                if field == 'webhook' and value and not webhook_token(value):
                    raise ValueError(f"{CHANNELS[channel]}需要有效的钉钉机器人 HTTPS Webhook")
                patch[channel][field] = value
        if self.path is None:
            raise OSError("通知地址配置存储不可用")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with interprocess_file_lock(self.path.with_suffix('.lock')):
            current = self.read()
            for channel, fields in patch.items():
                current[channel].update(fields)
            tokens = [webhook_token(current[c]['webhook']) for c in CHANNELS]
            if tokens[0] and tokens[0] == tokens[1]:
                raise ValueError("持仓与其他通知必须使用不同的机器人地址")
            saved = self._overrides()
            for channel in patch:
                # Freeze the whole edited channel, preserving unedited secrets.
                saved[channel] = current[channel]
            name = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                                 prefix='.notification-channels-', suffix='.tmp', delete=False) as stream:
                    name = stream.name
                    json.dump(saved, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if name is not None:
                    Path(name).unlink(missing_ok=True)
        return self._public(current)
