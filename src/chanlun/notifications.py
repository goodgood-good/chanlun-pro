"""Notification helpers shared by runtime tasks.

The live Chanlun monitor can reuse the DingTalk command configured for
Claude Code hooks without copying the robot token into this repository.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
from pathlib import Path
import threading
from typing import Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from chanlun import fun
from chanlun.decision_support.trading_system.file_lock import (
    InterprocessLockTimeout,
    interprocess_file_lock,
)


_DINGTALK_OUTBOUND_DEDUPE_SCHEMA = "chanlun-dingtalk-outbound-dedupe/v1"


def _iter_hook_commands(settings: dict, event_name: str) -> Iterable[str]:
    # 结构防御: ~/.claude/settings.json 允许 hooks/Notification 为 null 或非预期类型
    # (合法 JSON 但畸形结构), 裸 .get/迭代会抛 AttributeError/TypeError 逃逸 discover →
    # ClaudeHookNotifier.__init__ → 监控进程构造崩(F1-NOTIF-4)。任意畸形降级为空。
    root = settings.get("hooks") if isinstance(settings, dict) else None
    hooks = root.get(event_name) if isinstance(root, dict) else None
    if not isinstance(hooks, list):
        return
    for group in hooks:
        if not isinstance(group, dict):
            continue
        inner = group.get("hooks")
        if not isinstance(inner, list):
            continue
        for hook in inner:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command")
            if hook.get("type") == "command" and command:
                yield command


def discover_claude_notification_command(
    settings_path: Optional[os.PathLike | str] = None,
) -> Optional[str]:
    """Return the Claude Code Notification hook command, preferring DingTalk.

    The command is read from ``~/.claude/settings.json`` by default. Secrets
    remain in the existing hook script/config; callers only execute the command.
    """
    path = Path(
        settings_path
        or os.environ.get("CLAUDE_SETTINGS_PATH", "")
        or (Path.home() / ".claude" / "settings.json")
    )
    if not path.exists():
        return None
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fun.get_logger().warning(f"[notify] read Claude settings failed: {exc}")
        return None

    commands = list(_iter_hook_commands(settings, "Notification"))
    if not commands:
        return None
    for command in commands:
        if "dingtalk" in command.lower() or "dingding" in command.lower():
            return command
    return commands[0]


class DingTalkWebhookNotifier:
    """直发钉钉自定义机器人 webhook 的通知器(买卖点提醒专用通道)。

    机器人安全设置为「自定义关键词」时,消息文本必须包含该关键词才会被
    接受——send() 会在缺失时自动注入到消息首部。webhook 含 access_token,
    应只放在不入库的本地配置(config.py 在 .gitignore)。"""

    def __init__(
        self,
        webhook: str,
        keyword: str = "",
        timeout: int = 10,
        dry_run: bool = False,
        dry_run_collector: Callable[[str], None] | None = None,
        dedupe_state_path: Optional[os.PathLike | str] = None,
        dedupe_max_records: int = 10_000,
        rich_content_provider: (
            Callable[[Mapping[str, object]], Sequence[Mapping[str, str] | str]]
            | None
        ) = None,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if type(dry_run) is not bool:
            raise TypeError("dry_run must be boolean")
        if dry_run_collector is not None and not callable(dry_run_collector):
            raise TypeError("dry_run_collector must be callable")
        if rich_content_provider is not None and not callable(rich_content_provider):
            raise TypeError("rich_content_provider must be callable")
        if (
            isinstance(dedupe_max_records, bool)
            or not isinstance(dedupe_max_records, int)
            or dedupe_max_records <= 0
        ):
            raise ValueError("dedupe_max_records must be a positive integer")
        self.webhook = str(webhook or "")
        self.keyword = str(keyword or "")
        self.timeout = timeout
        self.dry_run = dry_run
        self._dry_run_collector = dry_run_collector
        self._dedupe_state_path = (
            None if dedupe_state_path is None else Path(dedupe_state_path)
        )
        self._dedupe_max_records = dedupe_max_records
        self._rich_content_provider = rich_content_provider
        self._dedupe_lock = threading.RLock()
        self._volatile_delivered: set[str] = set()

    @property
    def available(self) -> bool:
        return bool(self.webhook) or self.dry_run

    @staticmethod
    def _message_fingerprint(message: str) -> str:
        return "sha256:" + hashlib.sha256(message.encode("utf-8")).hexdigest()

    def _load_dedupe_records(self) -> dict[str, str]:
        path = self._dedupe_state_path
        if path is None:
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != _DINGTALK_OUTBOUND_DEDUPE_SCHEMA
            or not isinstance(payload.get("records"), dict)
        ):
            return {}
        return {
            str(fingerprint): str(sent_at)
            for fingerprint, sent_at in payload["records"].items()
            if isinstance(fingerprint, str)
            and fingerprint.startswith("sha256:")
            and len(fingerprint) == 71
            and isinstance(sent_at, str)
            and sent_at
        }

    def _persist_dedupe_records(self, records: dict[str, str]) -> None:
        path = self._dedupe_state_path
        if path is None:
            return
        retained = sorted(records.items(), key=lambda item: (item[1], item[0]))[
            -self._dedupe_max_records :
        ]
        payload = {
            "schema": _DINGTALK_OUTBOUND_DEDUPE_SCHEMA,
            "records": dict(retained),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _normalized_images(
        values: Sequence[Mapping[str, str] | str],
    ) -> tuple[tuple[str, str], ...]:
        images: list[tuple[str, str]] = []
        for index, raw in enumerate(values):
            if isinstance(raw, Mapping):
                url = str(raw.get("url") or "").strip()
                alt = str(raw.get("alt") or f"缠论结构图 {index + 1}").strip()
            else:
                url = str(raw or "").strip()
                alt = f"缠论结构图 {index + 1}"
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            # DingTalk markdown uses square brackets for alt text.  Keep stock
            # names readable while preventing them from terminating the image
            # expression supplied by this trusted notification renderer.
            alt = alt.replace("[", "（").replace("]", "）")
            images.append((url, alt))
        return tuple(images)

    def _send_message(
        self,
        message: str,
        images: tuple[tuple[str, str], ...] = (),
    ) -> bool:
        rendered = message
        if images:
            rendered = "  \n".join(message.splitlines())
            rendered += "\n\n" + "\n\n".join(
                f"![{alt}]({url})" for url, alt in images
            )
        if self.dry_run:
            if self._dry_run_collector is not None:
                self._dry_run_collector(rendered)
            return True
        if not self.webhook:
            fun.get_logger().warning("[notify] DingTalk webhook not configured")
            return False
        import urllib.request

        document = (
            {
                "msgtype": "markdown",
                "markdown": {
                    "title": message.splitlines()[0][:64] or "缠论买卖通知",
                    "text": rendered,
                },
            }
            if images
            else {"msgtype": "text", "text": {"content": message}}
        )
        payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
        try:
            req = urllib.request.Request(
                self.webhook,
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            errcode = int(data.get("errcode", -1))
        except Exception as exc:
            fun.get_logger().warning(
                f"[notify] DingTalk webhook failed: {type(exc).__name__}"
            )
            return False
        if errcode != 0:
            fun.get_logger().warning(
                f"[notify] DingTalk webhook rejected: errcode={data.get('errcode')}"
            )
            return False
        return True

    def _send_content(
        self,
        title: str,
        lines: list[str] | str,
        *,
        images: Sequence[Mapping[str, str] | str] = (),
    ) -> bool:
        body = lines if isinstance(lines, str) else "\n".join(str(x) for x in lines)
        message = f"{title}\n{body}" if body else str(title)
        if self.keyword and self.keyword not in message:
            message = f"[{self.keyword}] {message}"
        normalized_images = self._normalized_images(images)
        path = self._dedupe_state_path
        if path is None:
            return self._send_message(message, normalized_images)

        fingerprint = self._message_fingerprint(message)
        with self._dedupe_lock:
            if fingerprint in self._volatile_delivered:
                return True
            lock_path = path.with_suffix(path.suffix + ".lock")
            try:
                with interprocess_file_lock(lock_path, timeout_seconds=15.0):
                    records = self._load_dedupe_records()
                    if fingerprint in records:
                        self._volatile_delivered.add(fingerprint)
                        return True
                    sent = self._send_message(message, normalized_images)
                    if not sent:
                        return False
                    self._volatile_delivered.add(fingerprint)
                    records[fingerprint] = datetime.now(timezone.utc).isoformat()
                    try:
                        self._persist_dedupe_records(records)
                    except OSError as exc:
                        # The transport has already succeeded.  Keep the
                        # in-process barrier and avoid a false retry that would
                        # immediately duplicate the same DingTalk message.
                        fun.get_logger().warning(
                            f"[notify] outbound dedupe persist failed: {type(exc).__name__}"
                        )
                    return True
            except InterprocessLockTimeout as exc:
                # Fail closed: sending without the shared gate can create the
                # exact duplicate this barrier exists to prevent.
                fun.get_logger().warning(
                    f"[notify] outbound dedupe lock failed: {type(exc).__name__}"
                )
                return False

    def send(self, title: str, lines: list[str] | str) -> bool:
        return self._send_content(title, lines)

    def send_rich(
        self,
        title: str,
        lines: list[str] | str,
        context: Mapping[str, object],
    ) -> bool:
        """Send one Markdown alert with optional chart images.

        Rendering is best-effort.  If market data, the strict static renderer
        or the public image store is unavailable, the same event is delivered
        once as text; it is never followed by a second, duplicate image
        notification.
        """

        images: Sequence[Mapping[str, str] | str] = ()
        if self._rich_content_provider is not None:
            try:
                images = self._rich_content_provider(context)
            except Exception as exc:
                fun.get_logger().warning(
                    f"[notify] chart enrichment failed: {type(exc).__name__}"
                )
        return self._send_content(title, lines, images=images)


class ClaudeHookNotifier:
    """Send a text notification through the configured Claude Code hook."""

    def __init__(
        self,
        command: Optional[str] = None,
        settings_path: Optional[os.PathLike | str] = None,
        cwd: Optional[os.PathLike | str] = None,
        timeout: int = 12,
        dry_run: bool = False,
    ) -> None:
        self.command = (
            command
            or os.environ.get("CHANLUN_DINGTALK_HOOK_COMMAND")
            or discover_claude_notification_command(settings_path)
        )
        self.cwd = str(cwd or os.getcwd())
        self.timeout = timeout
        self.dry_run = dry_run

    @property
    def available(self) -> bool:
        return bool(self.command) or self.dry_run

    def send(self, title: str, lines: list[str] | str) -> bool:
        if isinstance(lines, str):
            body = lines
        else:
            body = "\n".join(str(line) for line in lines)
        message = f"{title}\n{body}" if body else title
        if self.dry_run:
            print(message)
            return True
        if not self.command:
            fun.get_logger().warning("[notify] Claude DingTalk hook command not found")
            return False

        payload = {
            "hook_event_name": "Notification",
            "cwd": self.cwd,
            "message": message,
        }
        try:
            proc = subprocess.run(
                self.command,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                shell=True,
                cwd=self.cwd,
                timeout=self.timeout,
                capture_output=True,
            )
        except Exception as exc:
            fun.get_logger().warning(f"[notify] hook command failed: {exc}")
            return False
        if proc.returncode != 0:
            fun.get_logger().warning(
                f"[notify] hook command exit={proc.returncode} stderr={proc.stderr[:300]}"
            )
            return False
        stderr = (proc.stderr or "").strip()
        if "dingtalk-notify error" in stderr.lower():
            fun.get_logger().warning(f"[notify] DingTalk hook error: {stderr[:300]}")
            return False
        try:
            body = json.loads(stderr) if stderr.startswith("{") else None
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("errcode", 0) != 0:
            fun.get_logger().warning(f"[notify] DingTalk api error: {stderr[:300]}")
            return False
        return True
