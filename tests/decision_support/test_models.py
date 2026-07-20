from __future__ import annotations

import dataclasses
from decimal import Decimal, localcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math

import pytest

from chanlun.decision_support.fingerprints import (
    build_event_id,
    canonical_json,
    sha256_json,
)
from chanlun.decision_support.models import (
    DecisionEvent,
    LevelSnapshot,
    MarketConstraints,
    StrategyTrack,
)


_SIGNAL_FINGERPRINT = "sha256:" + "0" * 64
_RULE_BINDING = {
    "rule_id": "chanlun.third_buy",
    "rule_card_version": 1,
    "rule_card_fingerprint": "sha256:" + "1" * 64,
    "rule_set_fingerprint": "sha256:" + "2" * 64,
    "corpus_manifest_fingerprint": "sha256:" + "3" * 64,
    "algorithm_fingerprint": "sha256:" + "4" * 64,
}
_STRATEGY_RUN_BINDING = {
    "strategy_run_id": "paper-run-" + "a" * 64,
    "strategy_run_epoch": 7,
    "strategy_run_fingerprint": "sha256:" + "b" * 64,
}


def _bound_event(event: DecisionEvent, **changes: object) -> DecisionEvent:
    binding = {**_RULE_BINDING, **changes}
    config_fingerprint = binding.pop(
        "config_fingerprint",
        event.config_fingerprint,
    )
    provenance_fingerprint = sha256_json(
        {**binding, "config_fingerprint": config_fingerprint}
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
        config_fingerprint=config_fingerprint,
        **binding,
    )


def _strategy_bound_event(
    event: DecisionEvent,
    **changes: object,
) -> DecisionEvent:
    rule_bound = _bound_event(event)
    strategy_binding = {**_STRATEGY_RUN_BINDING, **changes}
    provenance_fingerprint = sha256_json(
        {
            "schema_version": 1,
            "rule_provenance_fingerprint": rule_bound.provenance_fingerprint,
            "strategy_run": strategy_binding,
        }
    )
    event_id = build_event_id(
        rule_bound.market,
        rule_bound.code,
        rule_bound.signal_frequency,
        rule_bound.observed_at,
        rule_bound.signal.level,
        rule_bound.signal.bs_type,
        sha256_json(rule_bound.signal),
        provenance_fingerprint,
    )
    return replace(
        rule_bound,
        event_id=event_id,
        **strategy_binding,
    )


def test_sha256_json_is_independent_of_mapping_order() -> None:
    assert sha256_json({"b": 2, "a": 1}) == sha256_json({"a": 1, "b": 2})


def test_decision_event_is_frozen(make_decision_event) -> None:
    event = make_decision_event(track=StrategyTrack.TREND_CONTINUATION)

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.code = "SZ.000001"


def test_nested_sequences_are_copied_to_immutable_tuples(make_decision_event) -> None:
    event = make_decision_event()
    level = LevelSnapshot(
        "5m",
        1,
        "up",
        True,
        9.0,
        10.0,
        9.2,
        9.8,
        mmds=["3buy"],
        divergences=["pz"],
    )
    rebuilt = replace(event, levels=[level])

    assert isinstance(rebuilt.levels, tuple)
    assert rebuilt.levels[0].mmds == ("3buy",)
    assert rebuilt.levels[0].divergences == ("pz",)


@pytest.mark.parametrize(
    "changes",
    (
        {"live_divergence": True, "divergence_kind": None},
        {"live_divergence": True, "divergence_kind": "pz"},
        {"live_divergence": False, "confirmation_bs_type": "2buy"},
    ),
)
def test_signal_snapshot_rejects_inconsistent_divergence_metadata(
    make_decision_event,
    changes,
) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="inconsistent signal"):
        replace(event.signal, **changes)


def test_schema_v2_rejects_inconsistent_signal_metadata(
    make_decision_event,
) -> None:
    payload = make_decision_event().to_dict()
    payload["signal"]["live_divergence"] = True
    payload["signal"]["divergence_kind"] = None
    payload["signal"]["confirmation_bs_type"] = "2buy"

    with pytest.raises(ValueError, match="inconsistent signal"):
        DecisionEvent.from_dict(payload)


@pytest.mark.parametrize("direction", ("DOWN", "sideways", " down "))
def test_level_snapshot_rejects_unknown_direction(
    make_decision_event,
    direction,
) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="direction"):
        replace(event.levels[0], direction=direction)


def test_schema_v2_rejects_unknown_level_direction(make_decision_event) -> None:
    payload = make_decision_event().to_dict()
    payload["levels"][0]["direction"] = "DOWN"

    with pytest.raises(ValueError, match="direction"):
        DecisionEvent.from_dict(payload)


def test_level_snapshot_rejects_unknown_trade_gate_direction(
    make_decision_event,
) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="trade_gate_direction"):
        replace(event.levels[0], trade_gate_direction="sideways")


def test_event_round_trip_preserves_trade_gate_direction(
    make_decision_event,
) -> None:
    event = make_decision_event()
    levels = tuple(
        replace(
            level,
            source_frequency="1m",
            source_bar_closed_at=event.bar_closed_at,
            trade_gate_direction="down" if index == 0 else None,
        )
        for index, level in enumerate(event.levels)
    )
    source_bound = replace(event, levels=levels)

    payload = source_bound.to_dict()
    restored = DecisionEvent.from_dict(payload)

    assert payload["levels"][0]["trade_gate_direction"] == "down"
    assert restored == source_bound


def test_build_event_id_requires_signal_fingerprint() -> None:
    observed_at = datetime.fromisoformat("2026-07-13T10:35:00+08:00")

    with pytest.raises(TypeError):
        build_event_id("a", "SH.600519", "5m", observed_at, 1, "3buy")


def test_decision_event_round_trip_preserves_equality(make_decision_event) -> None:
    event = make_decision_event(
        live_divergence=True,
        confirmation_bs_type="2buy",
    )

    restored = DecisionEvent.from_dict(event.to_dict())

    assert restored == event
    assert restored.to_dict() == event.to_dict()


def test_schema_v3_round_trip_preserves_rule_binding(make_decision_event) -> None:
    event = _bound_event(make_decision_event())

    payload = event.to_dict()
    restored = DecisionEvent.from_dict(payload)

    assert payload["schema_version"] == 3
    assert restored == event
    assert restored.rule_binding_status == "bound"
    assert restored.to_dict() == payload


def test_schema_v4_round_trip_preserves_strategy_run_binding(
    make_decision_event,
) -> None:
    legacy = _bound_event(make_decision_event())
    event = _strategy_bound_event(make_decision_event())

    payload = event.to_dict()
    restored = DecisionEvent.from_dict(payload)

    assert payload["schema_version"] == 4
    assert {
        field_name: payload[field_name]
        for field_name in _STRATEGY_RUN_BINDING
    } == _STRATEGY_RUN_BINDING
    assert restored == event
    assert restored.strategy_run_binding_status == "bound"
    assert restored.rule_binding_status == "bound"
    assert restored.data_fingerprint == legacy.data_fingerprint
    assert restored.to_dict() == payload


@pytest.mark.parametrize("field_name", tuple(_STRATEGY_RUN_BINDING))
def test_strategy_run_binding_must_be_complete_or_absent(
    make_decision_event,
    field_name: str,
) -> None:
    event = _bound_event(make_decision_event())

    with pytest.raises(
        ValueError,
        match="strategy-run binding must be complete or absent",
    ):
        replace(event, **{field_name: _STRATEGY_RUN_BINDING[field_name]})


@pytest.mark.parametrize("epoch", [True, 0, -1, 1.5])
def test_strategy_run_epoch_must_be_a_positive_integer(
    make_decision_event,
    epoch: object,
) -> None:
    event = _bound_event(make_decision_event())

    with pytest.raises(
        ValueError,
        match="strategy_run_epoch must be a positive integer",
    ):
        replace(
            event,
            **{
                **_STRATEGY_RUN_BINDING,
                "strategy_run_epoch": epoch,
            },
        )


def test_strategy_run_fingerprint_must_be_canonical(
    make_decision_event,
) -> None:
    event = _bound_event(make_decision_event())

    with pytest.raises(ValueError, match="fingerprints must use sha256"):
        replace(
            event,
            **{
                **_STRATEGY_RUN_BINDING,
                "strategy_run_fingerprint": "not-a-fingerprint",
            },
        )


def test_strategy_bound_event_must_be_rule_bound(make_decision_event) -> None:
    with pytest.raises(
        ValueError,
        match="strategy-bound event must be rule-bound",
    ):
        replace(make_decision_event(), **_STRATEGY_RUN_BINDING)


def test_schema_v4_requires_every_strategy_run_field(
    make_decision_event,
) -> None:
    payload = _strategy_bound_event(make_decision_event()).to_dict()
    del payload["strategy_run_epoch"]

    with pytest.raises(ValueError, match="event fields mismatch"):
        DecisionEvent.from_dict(payload)


def test_schema_v3_rejects_partial_rule_binding(make_decision_event) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="rule binding must be complete"):
        replace(event, rule_id="chanlun.third_buy")


def test_schema_v2_round_trip_preserves_legacy_payload_shape(
    make_decision_event,
) -> None:
    event = make_decision_event()
    payload = event.to_dict()

    restored = DecisionEvent.from_dict(payload)

    assert payload["schema_version"] == 2
    assert restored.rule_binding_status == "legacy_unbound"
    assert restored.strategy_run_binding_status == "legacy_unbound"
    assert not set(_RULE_BINDING).intersection(payload)
    assert not set(_STRATEGY_RUN_BINDING).intersection(payload)
    assert restored.to_dict() == payload


@pytest.mark.parametrize("rule_bound", [False, True])
def test_legacy_event_canonical_fingerprint_omits_v4_binding_fields(
    make_decision_event,
    rule_bound: bool,
) -> None:
    event = make_decision_event()
    if rule_bound:
        event = _bound_event(event)
    legacy_projection = {
        field.name: getattr(event, field.name)
        for field in dataclasses.fields(event)
        if field.name not in _STRATEGY_RUN_BINDING
    }

    assert sha256_json(event) == sha256_json(legacy_projection)


@pytest.mark.parametrize("field_name", tuple(_RULE_BINDING))
def test_event_id_changes_for_each_rule_identity_component(
    make_decision_event,
    field_name: str,
) -> None:
    event = make_decision_event()
    first = _bound_event(event)
    value: object = "sha256:" + "f" * 64
    if field_name == "rule_id":
        value = "chanlun.third_buy.changed"
    elif field_name == "rule_card_version":
        value = 2

    changed = _bound_event(event, **{field_name: value})

    assert changed.event_id != first.event_id


def test_event_id_changes_when_config_fingerprint_changes(make_decision_event) -> None:
    event = make_decision_event()

    first = _bound_event(event)
    changed = _bound_event(
        event,
        config_fingerprint="sha256:" + "e" * 64,
    )

    assert changed.event_id != first.event_id


def test_event_rejects_provenance_suffix_mismatch(make_decision_event) -> None:
    event = _bound_event(make_decision_event())

    with pytest.raises(ValueError, match="event_id does not match event facts"):
        replace(event, rule_card_version=2)


def test_schema_v3_rejects_legacy_event_id(make_decision_event) -> None:
    legacy = make_decision_event()

    with pytest.raises(ValueError, match="event_id does not match event facts"):
        replace(legacy, **_RULE_BINDING)


def test_event_id_respects_database_length_limit() -> None:
    observed_at = datetime.fromisoformat("2026-07-13T10:35:00+08:00")

    with pytest.raises(ValueError, match="event_id exceeds database length"):
        build_event_id(
            "a",
            "X" * 200,
            "5m",
            observed_at,
            1,
            "3buy",
            _SIGNAL_FINGERPRINT,
            "sha256:" + "1" * 64,
        )


def test_decision_event_rejects_naive_observed_at(make_decision_event) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
        replace(event, observed_at=datetime(2026, 7, 13, 10, 35))


def test_market_constraints_reject_naive_quote_time() -> None:
    with pytest.raises(ValueError, match="quote_time must be timezone-aware"):
        MarketConstraints(
            board="main",
            lot=100,
            t_plus=1,
            limit_pct=0.10,
            entry_tradable=True,
            exit_tradable=True,
            quote_time=datetime(2026, 7, 13, 10, 35),
        )


def test_event_rejects_incomplete_level_snapshot(make_decision_event) -> None:
    event = make_decision_event()
    incomplete = replace(event.levels[0], completed=False)

    with pytest.raises(ValueError, match="event levels must use completed bars"):
        replace(event, levels=(incomplete,))


def test_event_rejects_mixed_legacy_and_source_bound_levels(
    make_decision_event,
) -> None:
    event = make_decision_event()
    source_bound = replace(
        event.levels[0],
        source_frequency="1m",
        source_bar_closed_at=event.bar_closed_at,
    )

    with pytest.raises(
        ValueError,
        match="event level source bindings must be complete or absent",
    ):
        replace(event, levels=(source_bound, event.levels[1]))


def test_event_rejects_source_bar_after_event_bar(make_decision_event) -> None:
    event = make_decision_event()
    levels = tuple(
        replace(
            level,
            source_frequency="1m",
            source_bar_closed_at=event.bar_closed_at,
        )
        for level in event.levels
    )
    future = replace(
        levels[0],
        source_bar_closed_at=event.bar_closed_at + timedelta(minutes=1),
    )

    with pytest.raises(
        ValueError,
        match="source bar cannot be after event bar",
    ):
        replace(event, levels=(future, levels[1]))


def test_event_rejects_inconsistent_watermarks_for_one_physical_source(
    make_decision_event,
) -> None:
    event = make_decision_event()
    first = replace(
        event.levels[0],
        source_frequency="1m",
        source_bar_closed_at=event.bar_closed_at,
    )
    second = replace(
        event.levels[1],
        source_frequency="1m",
        source_bar_closed_at=event.bar_closed_at - timedelta(minutes=1),
    )

    with pytest.raises(
        ValueError,
        match="one physical source must use one bar watermark",
    ):
        replace(event, levels=(first, second))


def test_event_rejects_stale_signal_source_watermark(
    make_decision_event,
) -> None:
    event = make_decision_event()
    stale_at = event.bar_closed_at - timedelta(minutes=1)
    levels = tuple(
        replace(
            level,
            source_frequency="1m",
            source_bar_closed_at=stale_at,
        )
        for level in event.levels
    )

    with pytest.raises(
        ValueError,
        match="signal source bar must match event bar",
    ):
        replace(event, levels=levels)


def test_canonical_json_rejects_non_finite_float() -> None:
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="finite"):
            canonical_json({"value": value})


def test_build_event_id_is_stable_and_requires_aware_time() -> None:
    observed_at = datetime.fromisoformat("2026-07-13T10:35:00+08:00")

    assert build_event_id(
        "a",
        "SH.600519",
        "5m",
        observed_at,
        1,
        "3buy",
        _SIGNAL_FINGERPRINT,
    ) == (
        "a:SH.600519:5m:2026-07-13T10:35:00+08:00:L1:3buy:S"
        + "0" * 64
    )
    with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
        build_event_id(
            "a",
            "SH.600519",
            "5m",
            datetime(2026, 7, 13, 10, 35),
            1,
            "3buy",
            _SIGNAL_FINGERPRINT,
        )

@pytest.mark.parametrize("field_name", ["level", "first_visible_bar", "nest_depth"])
def test_signal_integer_fields_require_int(make_decision_event, field_name: str) -> None:
    signal = make_decision_event().signal

    with pytest.raises(ValueError, match=f"{field_name} must be a non-negative integer"):
        replace(signal, **{field_name: 1.5})


def test_level_integer_field_requires_int(make_decision_event) -> None:
    level = make_decision_event().levels[0]

    with pytest.raises(ValueError, match="level must be a non-negative integer"):
        replace(level, level=1.5)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("price", "10.0"), ("zs_zd", True)],
)
def test_snapshot_numeric_fields_require_real_numbers(
    make_decision_event, field_name: str, value: object
) -> None:
    signal = make_decision_event().signal

    with pytest.raises(ValueError, match=f"{field_name} must be a finite number"):
        replace(signal, **{field_name: value})


def test_snapshot_boolean_fields_require_bool(make_decision_event) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="live_divergence must be boolean"):
        replace(event.signal, live_divergence=1)
    with pytest.raises(ValueError, match="completed must be boolean"):
        replace(event.levels[0], completed=1)
    with pytest.raises(ValueError, match="entry_tradable must be boolean"):
        replace(event.market_constraints, entry_tradable=1)


@pytest.mark.parametrize("field_name", ["mmds", "divergences"])
def test_level_label_sequences_require_non_empty_strings(
    make_decision_event, field_name: str
) -> None:
    level = make_decision_event().levels[0]

    with pytest.raises(
        ValueError, match=f"{field_name} must be a sequence of non-empty strings"
    ):
        replace(level, **{field_name: "3buy"})


def test_event_rejects_future_quote(make_decision_event) -> None:
    event = make_decision_event()
    future_constraints = replace(
        event.market_constraints,
        quote_time=event.observed_at + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="quote_time cannot be after observed_at"):
        replace(event, market_constraints=future_constraints)


def test_event_fingerprints_require_sha256(make_decision_event) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="fingerprints must use sha256"):
        replace(event, data_fingerprint="not-a-fingerprint")


def test_from_dict_rejects_non_string_identity(make_decision_event) -> None:
    payload = make_decision_event().to_dict()
    payload["event_id"] = 123

    with pytest.raises(ValueError, match="event_id must be a non-empty string"):
        DecisionEvent.from_dict(payload)


def test_from_dict_rejects_non_mapping_level(make_decision_event) -> None:
    payload = make_decision_event().to_dict()
    payload["levels"] = [1]

    with pytest.raises(ValueError, match="levels must contain mappings"):
        DecisionEvent.from_dict(payload)

def test_datetime_fields_reject_non_datetime_values(make_decision_event) -> None:
    constraints = make_decision_event().market_constraints

    with pytest.raises(ValueError, match="quote_time must be a datetime"):
        replace(constraints, quote_time="2026-07-13T10:35:00+08:00")

def test_numeric_equivalents_have_same_event_fingerprint(
    make_decision_event,
) -> None:
    integer_event = make_decision_event(price=10)
    floating_event = make_decision_event(price=10.0)

    assert integer_event == floating_event
    assert integer_event.to_dict() == floating_event.to_dict()
    assert sha256_json(integer_event) == sha256_json(floating_event)


def test_decimal_and_string_have_distinct_canonical_json() -> None:
    assert canonical_json(Decimal("10.0")) != canonical_json("10.0")


def test_equivalent_timezones_have_same_event_identity(
    make_decision_event,
) -> None:
    shanghai_time = datetime.fromisoformat("2026-07-13T10:35:00+08:00")
    utc_time = shanghai_time.astimezone(timezone.utc)
    shanghai_event = make_decision_event(observed_at=shanghai_time)
    utc_event = make_decision_event(observed_at=utc_time)

    assert build_event_id(
        "a", "SH.600519", "5m", shanghai_time, 1, "3buy", _SIGNAL_FINGERPRINT
    ) == build_event_id(
        "a", "SH.600519", "5m", utc_time, 1, "3buy", _SIGNAL_FINGERPRINT
    )
    assert shanghai_event == utc_event
    assert sha256_json(shanghai_event) == sha256_json(utc_event)


def test_from_dict_accepts_zulu_time_and_normalizes(make_decision_event) -> None:
    event = make_decision_event()
    payload = event.to_dict()
    payload["observed_at"] = "2026-07-13T02:35:00Z"
    payload["bar_closed_at"] = "2026-07-13T02:35:00Z"
    constraints = payload["market_constraints"]
    assert isinstance(constraints, dict)
    constraints["quote_time"] = "2026-07-13T02:35:00Z"

    assert DecisionEvent.from_dict(payload) == event


def test_build_event_id_rejects_delimiter_collisions() -> None:
    observed_at = datetime.fromisoformat("2026-07-13T10:35:00+08:00")

    with pytest.raises(ValueError, match="must not contain colon"):
        build_event_id(
            "a:b", "c", "5m", observed_at, 1, "3buy", _SIGNAL_FINGERPRINT
        )
    with pytest.raises(ValueError, match="must not contain colon"):
        build_event_id(
            "a", "b:c", "5m", observed_at, 1, "3buy", _SIGNAL_FINGERPRINT
        )


def test_decision_event_rejects_id_fact_mismatch(make_decision_event) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="event_id does not match event facts"):
        replace(event, code="SZ.000001")


def test_decision_event_rejects_id_level_frequency_mismatch(
    make_decision_event,
) -> None:
    event = make_decision_event()
    levels = tuple(
        replace(level, frequency="15m") if level.frequency == "5m" else level
        for level in event.levels
    )

    with pytest.raises(
        ValueError, match="event_id frequency and level must match event levels"
    ):
        replace(event, levels=levels)


def test_from_dict_rejects_unknown_and_missing_fields(
    make_decision_event,
) -> None:
    payload = make_decision_event().to_dict()
    payload["future_risk_fact"] = True
    with pytest.raises(ValueError, match="event fields mismatch"):
        DecisionEvent.from_dict(payload)

    payload = make_decision_event().to_dict()
    del payload["code"]
    with pytest.raises(ValueError, match="event fields mismatch"):
        DecisionEvent.from_dict(payload)


def test_sha256_json_returns_model_fingerprint() -> None:
    fingerprint = sha256_json({"data": "fixture"})

    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == len("sha256:") + 64


def test_event_dict_has_explicit_schema_version(make_decision_event) -> None:
    assert make_decision_event().to_dict()["schema_version"] == 2


def test_build_event_id_rejects_subsecond_time() -> None:
    observed_at = datetime.fromisoformat("2026-07-13T10:35:00.000001+08:00")

    with pytest.raises(ValueError, match="second precision"):
        build_event_id(
            "a",
            "SH.600519",
            "5m",
            observed_at,
            1,
            "3buy",
            _SIGNAL_FINGERPRINT,
        )


def test_decimal_canonicalization_is_exact_and_context_independent() -> None:
    value = Decimal("1.123456789012345678901234567890123456789")
    adjacent = Decimal("1.123456789012345678901234567890123456788")
    with localcontext() as context:
        context.prec = 28
        low_precision_hash = sha256_json(value)
        low_precision_adjacent_hash = sha256_json(adjacent)
    with localcontext() as context:
        context.prec = 50
        high_precision_hash = sha256_json(value)

    assert low_precision_hash == high_precision_hash
    assert low_precision_hash != low_precision_adjacent_hash


def test_decimal_canonical_tag_cannot_be_forged_by_mapping() -> None:
    assert canonical_json(Decimal("10")) != canonical_json(
        {"$decimal": "1E+1"}
    )


def test_event_value_objects_do_not_expose_mutable_dict(
    make_decision_event,
) -> None:
    event = make_decision_event()

    for value in (
        event,
        event.signal,
        event.levels[0],
        event.market_constraints,
    ):
        assert not hasattr(value, "__dict__")


def test_event_rejects_duplicate_frequency_level_snapshots(
    make_decision_event,
) -> None:
    event = make_decision_event()
    duplicate = replace(event.levels[1], direction="down")

    with pytest.raises(
        ValueError,
        match="level snapshots must have unique frequency and level",
    ):
        replace(event, levels=(*event.levels, duplicate))


@pytest.mark.parametrize("value", [2**53 + 1, 10**400])
def test_snapshot_rejects_integer_that_cannot_be_losslessly_float(
    make_decision_event,
    value: int,
) -> None:
    signal = make_decision_event().signal

    with pytest.raises(ValueError, match="price must be a finite number"):
        replace(signal, price=value)


@pytest.mark.parametrize("schema_version", [1.0, Decimal("1")])
def test_from_dict_requires_integer_schema_version(
    make_decision_event,
    schema_version: object,
) -> None:
    payload = make_decision_event().to_dict()
    payload["schema_version"] = schema_version

    with pytest.raises(ValueError, match="unsupported event schema_version"):
        DecisionEvent.from_dict(payload)
