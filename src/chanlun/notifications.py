"""Notification helpers shared by runtime tasks.

The live Chanlun monitor can reuse the DingTalk command configured for
Claude Code hooks without copying the robot token into this repository.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from chanlun import fun


def _iter_hook_commands(settings: dict, event_name: str) -> Iterable[str]:
    hooks = settings.get("hooks", {}).get(event_name, [])
    for group in hooks:
        for hook in group.get("hooks", []):
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
    ) -> None:
        self.webhook = str(webhook or "")
        self.keyword = str(keyword or "")
        self.timeout = timeout
        self.dry_run = dry_run

    @property
    def available(self) -> bool:
        return bool(self.webhook) or self.dry_run

    def send(self, title: str, lines: list[str] | str) -> bool:
        body = lines if isinstance(lines, str) else "\n".join(str(x) for x in lines)
        message = f"{title}\n{body}" if body else str(title)
        if self.keyword and self.keyword not in message:
            message = f"[{self.keyword}] {message}"
        if self.dry_run:
            print(message)
            return True
        if not self.webhook:
            fun.get_logger().warning("[notify] DingTalk webhook not configured")
            return False
        import urllib.request

        payload = json.dumps(
            {"msgtype": "text", "text": {"content": message}}, ensure_ascii=False
        ).encode("utf-8")
        req = urllib.request.Request(
            self.webhook,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            fun.get_logger().warning(f"[notify] DingTalk webhook failed: {exc}")
            return False
        if int(data.get("errcode", -1)) != 0:
            fun.get_logger().warning(
                f"[notify] DingTalk webhook rejected: {data.get('errcode')} {data.get('errmsg')}"
            )
            return False
        return True


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
