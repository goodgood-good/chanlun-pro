"""Idempotent lifecycle notifications for read-only trading signals."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path


SCHEMA_VERSION = "chanlun-signal-notifications/v1"
_NOTIFIABLE_TRANSITIONS = {
    (None, "triggered"),
    (None, "executable"),
    ("armed", "triggered"),
    ("triggered", "executable"),
    ("armed", "invalidated"),
    ("triggered", "invalidated"),
    ("active", "closed"),
}
_STAGE_LABELS = {
    "triggered": "精细触发",
    "executable": "满足执行条件",
    "invalidated": "结构失效",
    "closed": "持仓关闭",
}


def _signals_by_id(snapshot: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows = snapshot.get("signals", ())
    if not isinstance(rows, (list, tuple)):
        return {}
    output: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        signal_id = row.get("signal_id")
        if isinstance(signal_id, str) and signal_id:
            output[signal_id] = row
    return output


def _stage(signal: Mapping[str, object] | None) -> str | None:
    if signal is None:
        return None
    value = signal.get("lifecycle_stage")
    return value if isinstance(value, str) else None


def notification_event_id(signal_id: str, old_stage: str, new_stage: str) -> str:
    payload = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "signal_id": signal_id,
            "old_stage": old_stage,
            "new_stage": new_stage,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object, default: str = "—") -> str:
    rendered = str(value).strip() if value is not None else ""
    return rendered if rendered else default


def format_notification(
    signal: Mapping[str, object],
    old_stage: str,
    new_stage: str,
) -> tuple[str, list[str]]:
    context = _mapping(signal.get("context_30m"))
    setup = _mapping(signal.get("setup_5m"))
    trigger = _mapping(signal.get("trigger_1m"))
    sector = _mapping(signal.get("sector"))
    point_type = _text(signal.get("point_type"))
    code = _text(signal.get("code"))
    tower = _text(signal.get("tower"))
    level = _text(signal.get("recursive_level"), "0")
    title = (
        f"买卖通知｜{_STAGE_LABELS.get(new_stage, new_stage)}｜"
        f"{code} {point_type}"
    )
    old_stage_label = "首次发现" if old_stage == "None" else old_stage
    lines = [
        f"生命周期：{old_stage_label} → {new_stage}",
        f"结构层级：{tower} · L{level}",
        (
            "30m 大级别："
            f"{_text(context.get('direction'))} / "
            f"{_text(context.get('disposition'))}"
        ),
        (
            "5m 可操作级别："
            f"{_text(setup.get('point_type'), point_type)} / "
            f"第一中枢序号 {_text(setup.get('center_ordinal'))}"
        ),
        (
            "1m 精细触发："
            f"{_text(trigger.get('point_type'))} / "
            f"{_text(trigger.get('confirmed_at'))}"
        ),
        (
            "行业板块："
            f"{_text(sector.get('sector_name'))} / "
            f"{_text(sector.get('regime'))}"
        ),
        f"结构失效价：{_text(signal.get('structural_stop'))}",
        f"计划风险倍数：{_text(signal.get('risk_multiplier'))}",
        "只读提示：本通知不是下单指令，请结合成交约束人工复核。",
    ]
    return title, lines


class SignalNotificationDispatcher:
    def __init__(self, notifier: object, *, state_path: Path | None = None) -> None:
        send = getattr(notifier, "send", None)
        if not callable(send):
            raise TypeError("notifier must expose send")
        self._notifier = notifier
        self._state_path = None if state_path is None else Path(state_path)
        self._delivered = self._load_delivered()

    def _load_delivered(self) -> set[str]:
        if self._state_path is None:
            return set()
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return set()
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != SCHEMA_VERSION
            or not isinstance(payload.get("delivered_event_ids"), list)
        ):
            return set()
        values = payload["delivered_event_ids"]
        if not all(
            isinstance(value, str)
            and value.startswith("sha256:")
            and len(value) == 71
            for value in values
        ):
            return set()
        return set(values)

    def _persist(self, delivered: set[str]) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "delivered_event_ids": sorted(delivered),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._state_path)

    def dispatch_changes(
        self,
        previous: Mapping[str, object],
        current: Mapping[str, object],
    ) -> None:
        before = _signals_by_id(previous)
        for signal_id, document in sorted(_signals_by_id(current).items()):
            old_stage = _stage(before.get(signal_id))
            new_stage = _stage(document)
            transition = (old_stage, new_stage)
            if transition not in _NOTIFIABLE_TRANSITIONS:
                continue
            event_id = notification_event_id(
                signal_id,
                str(old_stage),
                str(new_stage),
            )
            if event_id in self._delivered:
                continue
            title, lines = format_notification(
                document,
                str(old_stage),
                str(new_stage),
            )
            if not self._notifier.send(title, lines):
                continue
            delivered = self._delivered | {event_id}
            self._persist(delivered)
            self._delivered = delivered


__all__ = [
    "SCHEMA_VERSION",
    "SignalNotificationDispatcher",
    "format_notification",
    "notification_event_id",
]
