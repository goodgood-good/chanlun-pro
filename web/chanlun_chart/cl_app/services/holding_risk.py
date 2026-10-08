"""User-bound structural price references, tracked beyond selection lifetime."""

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import re
from uuid import uuid4

import numpy as np

from chanlun.core.strict_structure.unit_adapter import _tick
from chanlun.screening.evidence import (
    load_evidence,
    evidence_native_signal_catalog,
    evidence_exit_catalog,
)


def slot(market, code, frequency):
    return f"{market}:{code}:{frequency}"


def signal_id(market, code, frequency, point_id):
    return hashlib.sha256(
        json.dumps([market, code, frequency, point_id]).encode()
    ).hexdigest()


def source(directory, record):
    run = record.get("evidence_run_id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", run):
        raise ValueError("缺少可核验的持仓行情")
    run_dir = directory / run
    frame, snapshot, health = load_evidence(
        run_dir, record["code"], record["frequency"], record["evidence"]
    )
    if (
        not health["verified"]
        or snapshot.get("analysis_scope") != "native_centers"
        or snapshot["source_closed_at"] != record["source_closed_at"]
    ):
        raise ValueError("持仓行情与结构截止不一致")
    points, centers, valid = evidence_native_signal_catalog(
        run_dir,
        record["code"],
        record["frequency"],
        record["evidence"],
        source_closed_at=record["source_closed_at"],
    )
    plans = evidence_exit_catalog(
        run_dir, record["code"], record["frequency"], record["evidence"]
    )
    return frame, snapshot, points, centers, valid, plans


def choices(directory, record):
    from .notification_text import price

    frame, snapshot, points, _, valid, plans = source(directory, record)
    quantum = Decimal(str(snapshot["structure_price_quantum"]))
    times = np.array([int(t.timestamp()) for t in frame.date])
    lows = np.array([_tick(value, quantum) for value in frame.low], dtype=np.int64)
    suffix = np.minimum.accumulate(lows[::-1])[::-1]
    output = []
    for point in points.values():
        stop = (plans.get(point["point_id"]) or {}).get("stop_loss", {})
        if (
            point.get("side") != "buy"
            or not valid.get(point["point_id"])
            or point.get("status") != "confirmed"
            or stop.get("status") != "reference"
            or stop.get("trigger") != "lt"
            or not isinstance(stop.get("price_tick"), int)
        ):
            continue
        index = int(np.searchsorted(times, point["anchor_at"], side="right"))
        if index < len(lows) and suffix[index] < stop["price_tick"]:
            continue
        output.append(
            {
                "signal_id": signal_id(
                    record["market"],
                    record["code"],
                    record["frequency"],
                    point["point_id"],
                ),
                "point_id": point["point_id"],
                "point_type": point["point_type"],
                "anchor_at": point["anchor_at"],
                "available_at": point["available_at"],
                "anchor_price": point["anchor_price"],
                "stop_price": stop["price"],
                "anchor_label": price(point["anchor_price"], str(quantum)),
                "stop_label": price(stop["price"], str(quantum)),
                "market": record["market"],
                "code": record["code"],
                "name": record.get("name", record["code"]),
                "frequency": record["frequency"],
                "source_closed_at": record["source_closed_at"],
            }
        )
    return sorted(output, key=lambda p: p["available_at"], reverse=True)[:20]


def bind(directory, record, identity, now):
    choice = next(
        (row for row in choices(directory, record) if row["signal_id"] == identity),
        None,
    )
    if choice is None:
        raise ValueError("该参考买点已不可用，请刷新后重新选择")
    frame, snapshot, points, centers, _, plans = source(directory, record)
    point = points[choice["point_id"]]
    # Binding is rare and explicit: independently replay the chosen historical
    # point even when it has already left the current screening window.
    from chanlun.screening.runner import _review_native_center_signals

    selected, rejected = _review_native_center_signals(
        snapshot,
        frame,
        [{"point": point, "center": centers[point["center_id"]], "reasons": []}],
        record["code"],
        record["frequency"],
    )
    if rejected or not selected:
        raise ValueError("该参考买点未通过确认时刻复核")
    return {
        **choice,
        "binding_id": uuid4().hex,
        "active": True,
        "bound_at": now,
        "bound_cutoff": record["source_closed_at"],
        "checked_cutoff": record["source_closed_at"],
        "price_basis_revision": snapshot["price_basis_revision"],
        "structure_price_quantum": snapshot["structure_price_quantum"],
        "stop_loss": deepcopy(plans[point["point_id"]]["stop_loss"]),
        "evidence_run_id": record["evidence_run_id"],
        "evidence": deepcopy(record["evidence"]),
        "checked_input_fingerprint": record["evidence"].get("input_fingerprint"),
        "status": "active",
        "stop_triggered_at": None,
        "reverse_point_id": None,
        "window_exited_at": None,
    }


def advance(plan, frame, snapshot, points, valid, active_ids, now):
    updated = deepcopy(plan)
    events = []
    cutoff = snapshot["source_closed_at"]
    if not plan.get("active") or cutoff < plan["checked_cutoff"]:
        return updated, events
    if snapshot["price_basis_revision"] != plan["price_basis_revision"]:
        updated.update(
            status="requires_review", detail="价格基准已变化，请重新绑定参考点"
        )
        return updated, events
    from chanlun.screening.cache import input_fingerprint

    fingerprint = input_fingerprint(frame, plan["code"], plan["frequency"])
    price_checked = cutoff == plan["checked_cutoff"] and fingerprint == plan.get(
        "checked_input_fingerprint"
    )
    if int(frame.date.iloc[0].timestamp()) > plan["checked_cutoff"]:
        updated.update(
            status="requires_review", detail="缺少连续跟踪所需的历史，请复核参考点"
        )
        return updated, events
    quantum = Decimal(str(snapshot["structure_price_quantum"]))
    old_quantum = Decimal(str(plan["structure_price_quantum"]))
    stop_price = Decimal(plan["stop_loss"]["price_tick"]) * old_quantum

    def event(kind, at, **detail):
        identity = hashlib.sha256(f"{plan['binding_id']}:{kind}".encode()).hexdigest()
        return {
            **{
                k: plan[k]
                for k in (
                    "market",
                    "code",
                    "name",
                    "frequency",
                    "binding_id",
                    "signal_id",
                    "point_id",
                )
            },
            "id": identity,
            "signal_type": "holding_risk",
            "point_type": kind,
            "stage": "triggered",
            "point_status": "triggered",
            "change": "discovered",
            "recorded_at": now,
            "available_at": at,
            "source_closed_at": cutoff,
            "reference_point_type": plan["point_type"],
            "anchor_price": plan["anchor_price"],
            "stop_price": plan["stop_price"],
            "latest_close": float(frame.close.iloc[-1]),
            "structure_price_quantum": str(old_quantum),
            "current_price_quantum": str(quantum),
            "price_basis_revision": plan["price_basis_revision"],
            "bound_cutoff": plan["bound_cutoff"],
            "stop_loss": deepcopy(plan["stop_loss"]),
            "holding": True,
            "external_notifications": True,
            **detail,
        }

    # Identical candles can reuse a price check, but replay evidence can recover
    # or change after a deployment without a new candle. Always check reversals.
    if not price_checked and plan.get("stop_triggered_at") is None:
        for candle in frame.itertuples(index=False):
            at = int(candle.date.timestamp())
            if (
                at > plan["bound_cutoff"]
                and Decimal(_tick(candle.low, quantum)) * quantum < stop_price
            ):
                updated["stop_triggered_at"] = at
                events.append(event("risk_stop", at, trigger_price=float(candle.low)))
                break
    if plan.get("reverse_point_id") is None:
        opposite = sorted(
            (
                p
                for p in points.values()
                if valid.get(p["point_id"])
                and p.get("side") == "sell"
                and p.get("status") == "confirmed"
                and p["anchor_at"] > plan["available_at"]
                and plan["bound_cutoff"] < p["available_at"] <= cutoff
            ),
            key=lambda p: (p["available_at"], p["point_id"]),
        )
        if opposite:
            point = opposite[0]
            updated["reverse_point_id"] = point["point_id"]
            updated["reverse_at"] = point["available_at"]
            events.append(
                event(
                    "risk_reverse",
                    point["available_at"],
                    reverse_point_type=point["point_type"],
                    reverse_point_id=point["point_id"],
                    reverse_price=point["anchor_price"],
                )
            )
    if plan["signal_id"] not in active_ids and not updated.get("window_exited_at"):
        updated["window_exited_at"] = cutoff
    updated.update(
        checked_cutoff=cutoff,
        checked_at=now,
        latest_close=float(frame.close.iloc[-1]),
        detail="",
        checked_input_fingerprint=fingerprint,
        status="stop_triggered"
        if updated.get("stop_triggered_at")
        else "reverse_confirmed"
        if updated.get("reverse_point_id")
        else "active",
    )
    return updated, events


def check(plan, directory, record, active_ids, now):
    """Compute outside the monitor lock; an optional reversal cannot block a stop."""
    try:
        frame, snapshot, points, centers, valid, _ = source(directory, record)
        try:
            verified, pending = verify_opposites(
                plan, frame, snapshot, points, centers, valid
            )
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            verified, pending = {}, True
        updated, events = advance(
            plan, frame, snapshot, points, verified, active_ids, now
        )
        for event in events:
            event.update(
                evidence=deepcopy(record["evidence"]),
                evidence_run_id=record["evidence_run_id"],
            )
        updated["reverse_check_pending"] = pending
        if pending and updated["status"] != "requires_review":
            updated["detail"] = "价格止损已检查；反向信号核验待重试"
        return updated, events, pending
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return (
            {**deepcopy(plan), "status": "waiting", "detail": "风险参考证据暂不可核验"},
            [],
            True,
        )


def verify_opposites(plan, frame, snapshot, points, centers, valid):
    if plan.get("reverse_point_id"):
        return valid, False
    candidates = [
        {"point": point, "center": centers[point["center_id"]], "reasons": []}
        for point in points.values()
        if valid.get(point["point_id"])
        and point.get("side") == "sell"
        and point.get("status") == "confirmed"
        and point["anchor_at"] > plan["available_at"]
        and point["available_at"] > plan["bound_cutoff"]
    ]
    if not candidates:
        return valid, False
    from chanlun.screening.runner import _review_native_center_signals

    selected, rejected = _review_native_center_signals(
        snapshot, frame, candidates, plan["code"], plan["frequency"]
    )
    checked = dict(valid)
    for item in rejected:
        checked[item["point"]["point_id"]] = False
    return checked, bool(rejected)
