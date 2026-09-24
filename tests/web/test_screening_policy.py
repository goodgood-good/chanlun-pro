"""The first-center preference filters selection without redefining points."""

import pytest

from cl_app.services.screening_policy import selection_policy_reasons


@pytest.mark.parametrize("ordinal", [2, 3, 9])
def test_later_upward_centers_are_excluded(ordinal):
    assert selection_policy_reasons({"point_type": "3buy", "center_ordinal": ordinal}) == [
        "THIRD_BUY_NOT_FIRST_UP_CENTER"]


def test_first_directional_center_does_not_require_a_second_center():
    point = {"point_type": "3buy", "center_ordinal": 1, "status": "confirmed",
             "global_chart_center_index": 12}
    assert selection_policy_reasons(point) == []
    assert point["status"] == "confirmed"


@pytest.mark.parametrize("ordinal", [None, 0, -1, True, "1"])
def test_unknown_center_order_is_not_assumed_to_be_first(ordinal):
    assert selection_policy_reasons({"point_type": "3buy", "center_ordinal": ordinal}) == [
        "THIRD_BUY_CENTER_SEQUENCE_UNKNOWN"]


@pytest.mark.parametrize("kind", ["1buy", "2buy", "1sell", "2sell", "3sell"])
def test_preference_is_specific_to_third_buys(kind):
    assert selection_policy_reasons({"point_type": kind, "center_ordinal": 3}) == []
