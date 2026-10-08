"""Non-trading U176 MACD zero-axis facts for a declared operating period."""

from __future__ import annotations

from datetime import datetime
import math


def macd_zero_axis_operational_state(
    bar_times,
    dif_values,
    dea_values,
    *,
    physical_frequency: str,
    as_of: datetime,
    closed_through: datetime | None,
    bar_known_times=None,
    declared_minimum_frequency: str | None = None,
) -> dict:
    """Keep L103:40-76's chosen-period risk filter separate from structure.

    Both DIF and DEA must be strictly below zero for the basic avoid state.
    Contact with zero or a mixed pair remains a boundary observation.  The
    selected minimum period is explicit input; chart period alone is not it.
    """

    if (not isinstance(physical_frequency, str) or not physical_frequency
            or (declared_minimum_frequency is not None
                and (not isinstance(declared_minimum_frequency, str)
                     or not declared_minimum_frequency))
            or as_of.tzinfo is None or as_of.utcoffset() is None):
        raise ValueError("MACD operating risk requires an aware cutoff and explicit period names")

    def state(status, *, bar_at=None, known_at=None, dif=None, dea=None,
              axis=None, policy="not_applied_without_declared_minimum_period"):
        return {
            "rule_id": "U176", "rule_source_ref": "L103:40-76",
            "physical_frequency": physical_frequency,
            "declared_minimum_frequency": declared_minimum_frequency,
            "indicator_state": status,
            "risk_filter_state": policy,
            "bar_market_at": bar_at, "known_at": known_at,
            "dif": dif, "dea": dea,
            "zero_axis_boundary": axis,
            "formal_structure_independent": True,
            "higher_skill_divergence_path_independent": True,
            "tradable": False,
        }

    if (closed_through is None or closed_through.tzinfo is None
            or closed_through.utcoffset() is None or closed_through > as_of):
        return state("closed_bar_unavailable", policy="pending_closed_bar")
    times = tuple(bar_times)
    dif = tuple(dif_values)
    dea = tuple(dea_values)
    known_times = None if bar_known_times is None else tuple(bar_known_times)
    if (not times or len(times) != len(dif) or len(times) != len(dea)
            or known_times is not None and len(times) != len(known_times)
            or any(time.tzinfo is None or time.utcoffset() is None for time in times)
            or any(left >= right for left, right in zip(times, times[1:]))):
        return state("indicator_alignment_unavailable", policy="pending_indicator_evidence")
    index = next((i for i in range(len(times) - 1, -1, -1)
                  if times[i] <= closed_through), None)
    if index is None:
        return state("closed_bar_unavailable", policy="pending_closed_bar")
    known_at = closed_through if known_times is None else known_times[index]
    if (known_at is None or known_at.tzinfo is None or known_at.utcoffset() is None
            or not times[index] <= known_at <= as_of):
        return state("closed_bar_unavailable", policy="pending_closed_bar")
    try:
        dif_now, dea_now = float(dif[index]), float(dea[index])
    except (TypeError, ValueError):
        return state("indicator_unavailable", policy="pending_indicator_evidence")
    if not math.isfinite(dif_now) or not math.isfinite(dea_now):
        return state("indicator_unavailable", policy="pending_indicator_evidence")

    if dif_now < 0 and dea_now < 0:
        indicator, boundary = "both_below_zero", False
    elif dif_now > 0 and dea_now > 0:
        indicator, boundary = "both_above_zero", False
    else:
        indicator, boundary = "mixed_or_on_zero", True
    if declared_minimum_frequency is None:
        policy = "not_applied_without_declared_minimum_period"
    elif declared_minimum_frequency != physical_frequency:
        policy = "not_applicable_to_other_period"
    elif indicator == "both_below_zero":
        policy = "basic_avoid_on_selected_period"
    elif indicator == "both_above_zero":
        policy = "basic_zero_axis_risk_not_triggered"
    else:
        policy = "selected_period_zero_axis_boundary_pending"
    return state(indicator, bar_at=times[index], known_at=known_at,
                 dif=dif_now, dea=dea_now, axis=boundary, policy=policy)


def apply_declared_minimum_operating_frequency(
    saved_fact: dict, *, physical_frequency: str,
    minimum_frequency: str, source_closed_at: int,
) -> dict:
    """Apply an explicit user period to a verified frozen U176 indicator fact."""

    if (not isinstance(saved_fact, dict)
            or type(minimum_frequency) is not str or not minimum_frequency
            or type(source_closed_at) is not int
            or saved_fact.get("rule_id") != "U176"
            or saved_fact.get("physical_frequency") != physical_frequency
            or saved_fact.get("declared_minimum_frequency") is not None
            or saved_fact.get("risk_filter_state") != "not_applied_without_declared_minimum_period"
            or saved_fact.get("formal_structure_independent") is not True
            or saved_fact.get("tradable") is not False):
        raise ValueError("frozen U176 indicator fact is missing or already policy-modified")
    indicator = saved_fact.get("indicator_state")
    if indicator in {"both_below_zero", "both_above_zero", "mixed_or_on_zero"}:
        market_at, known_at = saved_fact.get("bar_market_at"), saved_fact.get("known_at")
        if (type(market_at) is not int or type(known_at) is not int
                or not market_at <= known_at <= source_closed_at):
            raise ValueError("frozen U176 fact used a future or invalid clock")
        try:
            dif, dea = float(saved_fact["dif"]), float(saved_fact["dea"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("frozen U176 fact lacks DIF or DEA") from exc
        if not math.isfinite(dif) or not math.isfinite(dea):
            raise ValueError("frozen U176 fact contains a nonfinite indicator")
        expected = ("both_below_zero" if dif < 0 and dea < 0 else
                    "both_above_zero" if dif > 0 and dea > 0 else
                    "mixed_or_on_zero")
        if expected != indicator:
            raise ValueError("frozen U176 indicator state contradicts DIF/DEA")
    elif indicator not in {"closed_bar_unavailable", "indicator_alignment_unavailable",
                           "indicator_unavailable"}:
        raise ValueError("frozen U176 indicator state is unknown")

    if minimum_frequency != physical_frequency:
        policy = "not_applicable_to_other_period"
    elif indicator == "both_below_zero":
        policy = "basic_avoid_on_selected_period"
    elif indicator == "both_above_zero":
        policy = "basic_zero_axis_risk_not_triggered"
    elif indicator == "mixed_or_on_zero":
        policy = "selected_period_zero_axis_boundary_pending"
    else:
        policy = "pending_indicator_evidence"
    return {**saved_fact, "declared_minimum_frequency": minimum_frequency,
            "risk_filter_state": policy}
