"""User selection preferences, independent of immutable chart definitions.

The engine's center_ordinal counts within a directional center sequence and
resets at its declared boundaries. It is not the chart's global center index.
Including its first upward breakout does not assert that a one-center start
already meets the original-text definition of a completed upward trend.
"""

POLICY_VERSION = "first-up-center-third-buy-v1"
POLICY_LABELS = {
    "THIRD_BUY_NOT_FIRST_UP_CENTER": "三买不属于向上方向序列的第一个中枢，按当前选股偏好排除",
    "THIRD_BUY_CENTER_SEQUENCE_UNKNOWN": "缺少可核对的三买中枢序号，无法确认是否首中枢",
}


def selection_policy_reasons(point):
    if point.get("point_type") != "3buy":
        return []
    ordinal = point.get("center_ordinal")
    if type(ordinal) is not int or ordinal < 1:
        return ["THIRD_BUY_CENTER_SEQUENCE_UNKNOWN"]
    if ordinal != 1:
        return ["THIRD_BUY_NOT_FIRST_UP_CENTER"]
    return []
