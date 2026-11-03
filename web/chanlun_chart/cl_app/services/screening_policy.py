"""Selection preferences, independent of immutable chart definitions.

Native signals require a separately derived causal sequence. The historical
center_ordinal field is a local pair counter and is not first-center evidence.
The frozen chart point remains unchanged when the preference excludes it.

L091:1045-1051 calls a third buy after the second center generally lacking
*new* entry value when the first-center third buy was already used. This
product filter selects first-center third buys regardless of position history;
it is a preference, not an author rule invalidating later formal points.
"""

POLICY_VERSION = "first-up-center-third-buy-v2"
POLICY_LABELS = {
    "THIRD_BUY_NOT_FIRST_UP_CENTER": "三买不属于向上方向序列的第一个中枢，按当前选股偏好排除",
    "THIRD_BUY_CENTER_SEQUENCE_UNKNOWN": "上涨起点或前序中枢证据未核验，不能当作首中枢三买",
}


def selection_policy_reasons(point, sequence=None):
    if point.get("point_type") != "3buy":
        return []
    if point.get('evidence_scope') == 'native_center_signal':
        from .first_center_sequence import first_verified, SCHEMA
        if (not isinstance(sequence, dict) or sequence.get('schema') != SCHEMA
                or sequence.get('center_id') != point.get('center_id')
                or sequence.get('known_at') != point.get('available_at')):
            return ['THIRD_BUY_CENTER_SEQUENCE_UNKNOWN']
        if first_verified(sequence) and sequence.get('direction') == 'up':
            return []
        if sequence.get('state') == 'later':
            return ['THIRD_BUY_NOT_FIRST_UP_CENTER']
        return ['THIRD_BUY_CENTER_SEQUENCE_UNKNOWN']
    ordinal = point.get("center_ordinal")
    if type(ordinal) is not int or ordinal < 1:
        return ["THIRD_BUY_CENTER_SEQUENCE_UNKNOWN"]
    if ordinal != 1:
        return ["THIRD_BUY_NOT_FIRST_UP_CENTER"]
    return []
