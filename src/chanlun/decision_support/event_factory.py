from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Mapping, Sequence

from chanlun.recursive_bt.engine.engine import (
    Signal,
    collect_branch_signals,
    collect_nest_cascade_signals,
    collect_qs_beichi_candidates,
)

from .fingerprints import build_event_id, normalize_datetime, sha256_json
from .models import (
    DecisionEvent,
    LevelSnapshot,
    MarketConstraints,
    SignalSnapshot,
    StrategyTrack,
    normalize_finite_float,
)
from .rule_cards import RuleEvaluation


def _as_float(value: object, field_name: str) -> float | None:
    return normalize_finite_float(value, field_name)


def _required_float(value: object, field_name: str) -> float:
    normalized = normalize_finite_float(value, field_name, optional=False)
    if normalized is None:
        raise AssertionError(f"required {field_name} was not normalized")
    return normalized


def _normalized_time(value: object, field_name: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    return normalize_datetime(value, field_name)


def bind_rule_evaluation(
    event: DecisionEvent,
    evaluation: RuleEvaluation,
) -> DecisionEvent:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be a DecisionEvent")
    if not isinstance(evaluation, RuleEvaluation):
        raise TypeError("evaluation must be a RuleEvaluation")
    if event.strategy_track is not evaluation.strategy_track:
        raise ValueError("rule evaluation strategy track mismatch")
    if event.signal.level != evaluation.level:
        raise ValueError("rule evaluation signal level mismatch")
    binding = {
        "rule_id": evaluation.rule_id,
        "rule_card_version": evaluation.rule_card_version,
        "rule_card_fingerprint": evaluation.rule_card_fingerprint,
        "rule_set_fingerprint": evaluation.rule_set_fingerprint,
        "corpus_manifest_fingerprint": evaluation.corpus_manifest_fingerprint,
        "algorithm_fingerprint": evaluation.algorithm_fingerprint,
    }
    if event.rule_binding_status == "bound":
        if (
            event.data_fingerprint
            == evaluation.evaluation_input_fingerprint
            and all(
                getattr(event, field_name) == value
                for field_name, value in binding.items()
            )
        ):
            return event
        raise ValueError("event already has a different rule binding")
    provenance_fingerprint = sha256_json(
        {**binding, "config_fingerprint": event.config_fingerprint}
    )
    event_id = build_event_id(
        event.market,
        event.code,
        event.signal_frequency,
        event.observed_at,
        event.signal.level,
        event.signal.bs_type,
        sha256_json(event.signal),
        provenance_fingerprint,
    )
    return replace(
        event,
        event_id=event_id,
        data_fingerprint=evaluation.evaluation_input_fingerprint,
        **binding,
    )


def bind_strategy_run_provenance(
    event: DecisionEvent,
    *,
    strategy_run_id: str,
    strategy_run_epoch: int,
    strategy_run_fingerprint: str,
) -> DecisionEvent:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be a DecisionEvent")
    if event.rule_binding_status != "bound":
        raise ValueError("strategy-run provenance requires a rule-bound event")
    binding = {
        "strategy_run_id": strategy_run_id,
        "strategy_run_epoch": strategy_run_epoch,
        "strategy_run_fingerprint": strategy_run_fingerprint,
    }
    if event.strategy_run_binding_status == "bound":
        if all(
            getattr(event, field_name) == value
            for field_name, value in binding.items()
        ):
            return event
        raise ValueError("event already has a different strategy-run binding")
    provenance_fingerprint = sha256_json(
        {
            "schema_version": 1,
            "rule_provenance_fingerprint": event.provenance_fingerprint,
            "strategy_run": binding,
        }
    )
    event_id = build_event_id(
        event.market,
        event.code,
        event.signal_frequency,
        event.observed_at,
        event.signal.level,
        event.signal.bs_type,
        sha256_json(event.signal),
        provenance_fingerprint,
    )
    return replace(
        event,
        event_id=event_id,
        **binding,
    )


def _last(sequence: object) -> object | None:
    try:
        values = list(sequence or ())
    except TypeError:
        return None
    return values[-1] if values else None


def _point_value(value: object, endpoint: str) -> float | None:
    explicit = getattr(value, f"segment_{endpoint}", None)
    if explicit is not None:
        return _as_float(explicit, f"segment_{endpoint}")
    segment = _last(getattr(value, "zslxs", ()))
    point = getattr(segment, endpoint, None)
    point_value = getattr(point, "val", None)
    return _as_float(point_value, f"segment_{endpoint}")


def _level_direction(value: object) -> str:
    explicit = getattr(value, "direction", None)
    if explicit is not None:
        return str(explicit)
    segment = _last(getattr(value, "zslxs", ()))
    direction = getattr(segment, "type", getattr(segment, "_type", None))
    if direction in {"up", "down"}:
        return str(direction)
    unit = _last(getattr(value, "units", ()))
    direction = getattr(unit, "type", getattr(unit, "_type", None))
    return str(direction) if direction in {"up", "down"} else "neutral"


def _level_frequency(cd: object, value: object, level: int) -> str:
    explicit = getattr(value, "frequency", None)
    if explicit is not None:
        return str(explicit)
    base = str(getattr(cd, "frequency", ""))
    if level == 0 and base:
        return base
    chain = getattr(cd, "_UPGRADE_CHAIN", {}).get(base, ())
    if 0 < level <= len(chain):
        return str(chain[level - 1][0])
    return f"L{level}"


def _level_labels(value: object, field_name: str) -> tuple[str, ...]:
    explicit = getattr(value, field_name, None)
    if explicit is not None:
        return tuple(str(item) for item in explicit if str(item))
    if field_name == "divergences":
        labels = []
        for divergence in getattr(value, "done_divergence", ()) or ():
            if divergence is None or not getattr(divergence, "is_beichi", False):
                continue
            kind = str(getattr(divergence, "kind", ""))
            if kind:
                labels.append(kind)
        return tuple(labels)
    return ()


def snapshot_levels(
    cd: object,
    *,
    source_frequency: str | None = None,
    source_bar_closed_at: datetime | None = None,
) -> tuple[LevelSnapshot, ...]:
    if not callable(getattr(cd, "get_recursive_branch_levels", None)):
        raise TypeError("cd must expose get_recursive_branch_levels")
    snapshots: list[LevelSnapshot] = []
    for value in cd.get_recursive_branch_levels():
        level = int(getattr(value, "level"))
        latest_zs = _last(getattr(value, "zss", ()))
        zs_zd = getattr(value, "zs_zd", None)
        zs_zg = getattr(value, "zs_zg", None)
        if latest_zs is not None:
            zs_zd = getattr(latest_zs, "zd", zs_zd)
            zs_zg = getattr(latest_zs, "zg", zs_zg)
        snapshots.append(
            LevelSnapshot(
                frequency=_level_frequency(cd, value, level),
                level=level,
                direction=_level_direction(value),
                completed=getattr(value, "completed", True),
                segment_start=_point_value(value, "start"),
                segment_end=_point_value(value, "end"),
                zs_zd=_as_float(zs_zd, "zs_zd"),
                zs_zg=_as_float(zs_zg, "zs_zg"),
                mmds=_level_labels(value, "mmds"),
                divergences=_level_labels(value, "divergences"),
                source_frequency=source_frequency,
                source_bar_closed_at=source_bar_closed_at,
            )
        )
    return tuple(snapshots)


def signal_snapshot(
    signal: Signal,
    first_visible_bar: int,
) -> SignalSnapshot:
    if not isinstance(signal, Signal):
        raise TypeError("signal must be Signal")
    _validate_signal_metadata(signal)
    return SignalSnapshot(
        bs_type=signal.bs_type,
        signal_at=_normalized_time(signal.date, "signal.date"),
        level=int(signal.level or 0),
        price=normalize_finite_float(
            signal.price,
            "signal.price",
            optional=False,
        ),
        first_visible_bar=first_visible_bar,
        structural_stop_below=signal.structural_stop_below,
        structural_stop_above=signal.structural_stop_above,
        zs_zd=signal.zs_zd,
        zs_zg=signal.zs_zg,
        nest_operable=signal.nest_operable,
        nest_depth=int(signal.nest_depth or 0),
        divergence_kind=signal.divergence_kind,
        live_divergence=signal.live_divergence,
        confirmation_bs_type=signal.confirmation_bs_type,
    )


def _validate_signal_metadata(signal: Signal) -> None:
    if type(signal.live_divergence) is not bool:
        raise ValueError("live_divergence must be boolean")
    if signal.live_divergence and signal.divergence_kind != "qs":
        raise ValueError("inconsistent signal divergence metadata")
    if signal.confirmation_bs_type is not None and not signal.live_divergence:
        raise ValueError("inconsistent signal confirmation metadata")


def _merge_optional(first: object, second: object) -> object:
    if first is None:
        return second
    if second is None or first == second:
        return first
    raise ValueError("conflicting signal metadata")


def _merge_default_zero(first: int, second: int) -> int:
    if first == 0:
        return second
    if second == 0 or first == second:
        return first
    raise ValueError("conflicting signal metadata")


def _normalized_signal(signal: Signal) -> Signal:
    if not isinstance(signal, Signal):
        raise TypeError("collectors must return Signal values")
    normalized = Signal(
        date=_normalized_time(signal.date, "signal.date"),
        level=int(signal.level or 0),
        bs_type=str(signal.bs_type),
        price=_required_float(signal.price, "signal.price"),
        nest_operable=signal.nest_operable,
        nest_depth=int(signal.nest_depth or 0),
        structural_stop_below=_as_float(
            signal.structural_stop_below,
            "signal.structural_stop_below",
        ),
        structural_stop_above=_as_float(
            signal.structural_stop_above,
            "signal.structural_stop_above",
        ),
        zs_zd=_as_float(signal.zs_zd, "signal.zs_zd"),
        zs_zg=_as_float(signal.zs_zg, "signal.zs_zg"),
        divergence_kind=signal.divergence_kind,
        live_divergence=signal.live_divergence,
        confirmation_bs_type=signal.confirmation_bs_type,
    )
    _validate_signal_metadata(normalized)
    return normalized


def _merge_signals(first: Signal, second: Signal) -> Signal:
    merged = Signal(
        date=first.date,
        level=first.level,
        bs_type=first.bs_type,
        price=first.price,
        nest_operable=_merge_optional(
            first.nest_operable,
            second.nest_operable,
        ),
        nest_depth=_merge_default_zero(first.nest_depth, second.nest_depth),
        structural_stop_below=_merge_optional(
            first.structural_stop_below,
            second.structural_stop_below,
        ),
        structural_stop_above=_merge_optional(
            first.structural_stop_above,
            second.structural_stop_above,
        ),
        zs_zd=_merge_optional(first.zs_zd, second.zs_zd),
        zs_zg=_merge_optional(first.zs_zg, second.zs_zg),
        divergence_kind=_merge_optional(
            first.divergence_kind,
            second.divergence_kind,
        ),
        live_divergence=first.live_divergence or second.live_divergence,
        confirmation_bs_type=_merge_optional(
            first.confirmation_bs_type,
            second.confirmation_bs_type,
        ),
    )
    _validate_signal_metadata(merged)
    return merged


def visible_signals(cd: object) -> tuple[Signal, ...]:
    collected = (
        *collect_branch_signals(cd, use_xd=False, annotate_nest=True),
        *collect_nest_cascade_signals(cd),
        *collect_qs_beichi_candidates(cd),
    )
    unique: dict[tuple[object, ...], Signal] = {}
    for raw_signal in collected:
        signal = _normalized_signal(raw_signal)
        key = (
            signal.date,
            int(signal.level or 0),
            str(signal.bs_type),
            signal.price,
        )
        if key in unique:
            unique[key] = _merge_signals(unique[key], signal)
        else:
            unique[key] = signal
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (item[0], item[1], item[2], item[3]),
        )
    )


def _completed_bar_snapshot(
    completed_bars: Sequence[Mapping[str, object]],
    bar_closed_at: datetime,
) -> tuple[Mapping[str, object], ...]:
    if isinstance(completed_bars, (str, bytes)) or not isinstance(
        completed_bars,
        Sequence,
    ):
        raise TypeError("completed_bars must be a sequence")
    if not completed_bars:
        raise ValueError("completed_bars cannot be empty")

    normalized_bars: list[Mapping[str, object]] = []
    previous_time: datetime | None = None
    for index, bar in enumerate(completed_bars):
        if not isinstance(bar, Mapping):
            raise TypeError("completed_bars must contain mappings")
        time_keys = tuple(
            key
            for key in (
                "closed_at",
                "time",
                "date",
                "datetime",
                "timestamp",
            )
            if key in bar
        )
        if len(time_keys) != 1:
            raise ValueError("completed bar must contain exactly one time field")
        time_key = time_keys[0]
        bar_time = _normalized_time(bar[time_key], f"completed_bars[{index}].time")
        value_fields = ("open", "high", "low", "close", "volume")
        if not set(value_fields).issubset(bar):
            raise ValueError("completed bar must contain OHLCV fields")
        values: dict[str, float] = {}
        for field_name in value_fields:
            number = normalize_finite_float(
                bar[field_name],
                f"completed bar {field_name}",
                optional=False,
            )
            if number is None:
                raise AssertionError("required OHLCV value was not normalized")
            values[field_name] = number
        if any(values[field_name] <= 0 for field_name in ("open", "high", "low", "close")):
            raise ValueError("completed bar prices must be positive")
        if values["volume"] < 0:
            raise ValueError("completed bar volume must be non-negative")
        if not (
            values["low"] <= values["open"] <= values["high"]
            and values["low"] <= values["close"] <= values["high"]
        ):
            raise ValueError("completed bar price range is inconsistent")
        if bar_time > bar_closed_at:
            raise ValueError("completed bar cannot be after bar_closed_at")
        if previous_time is not None and bar_time <= previous_time:
            raise ValueError("completed_bars must be strictly chronological")
        previous_time = bar_time
        normalized_bars.append(
            {
                **{
                    key: value
                    for key, value in bar.items()
                    if key not in time_keys and key not in value_fields
                },
                **values,
                "closed_at": bar_time,
            }
        )
    if previous_time != bar_closed_at:
        raise ValueError("latest completed bar must match bar_closed_at")
    return tuple(normalized_bars)


def event_from_signal(
    *,
    market: str,
    code: str,
    name: str,
    frequency: str,
    signal: Signal,
    first_visible_bar: int,
    observed_at: datetime,
    bar_closed_at: datetime,
    operation_bar_closed: bool,
    cd: object,
    market_constraints: MarketConstraints,
    completed_bars: Sequence[Mapping[str, object]],
    config: Mapping[str, object],
    strategy_track: StrategyTrack,
) -> DecisionEvent | None:
    if not isinstance(operation_bar_closed, bool):
        raise ValueError("operation_bar_closed must be boolean")
    if not operation_bar_closed:
        return None
    if not isinstance(market_constraints, MarketConstraints):
        raise TypeError("market_constraints must be MarketConstraints")
    if not isinstance(config, Mapping):
        raise TypeError("config must be a mapping")

    processing_at = normalize_datetime(observed_at, "observed_at")
    bar_closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
    if bar_closed_at > processing_at:
        raise ValueError("bar_closed_at cannot be after observed_at")
    signal_date = _normalized_time(signal.date, "signal.date")
    if signal_date > bar_closed_at:
        raise ValueError("signal date cannot be after bar_closed_at")
    completed_bar_snapshot = _completed_bar_snapshot(
        completed_bars,
        bar_closed_at,
    )

    frozen_signal = signal_snapshot(signal, first_visible_bar)
    levels = snapshot_levels(
        cd,
        source_frequency=str(getattr(cd, "frequency", "")),
        source_bar_closed_at=bar_closed_at,
    )
    data_fingerprint = sha256_json(
        {
            "market": market,
            "code": code,
            "frequency": frequency,
            "bar_closed_at": bar_closed_at,
            "completed_bars": completed_bar_snapshot,
            "signal": frozen_signal,
            "levels": levels,
        }
    )
    config_fingerprint = sha256_json(config)
    return DecisionEvent(
        event_id=build_event_id(
            market,
            code,
            frequency,
            bar_closed_at,
            frozen_signal.level,
            frozen_signal.bs_type,
            sha256_json(frozen_signal),
        ),
        market=market,
        code=code,
        name=name,
        observed_at=bar_closed_at,
        bar_closed_at=bar_closed_at,
        strategy_track=strategy_track,
        signal=frozen_signal,
        levels=levels,
        market_constraints=market_constraints,
        data_fingerprint=data_fingerprint,
        config_fingerprint=config_fingerprint,
    )
