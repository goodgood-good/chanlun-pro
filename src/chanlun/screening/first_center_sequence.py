"""Causal first-center admission for the existing native operating framework.

Read local author text in D:/缠论/chanlun_lesson_corpus:
L091_*:1045-1051 and L029_*:2131-2137 discuss the first center of a movement,
not a counter restarted at every overlapping pair. L020_*:148-163 distinguishes
extension, new centers and expansion. The conservative reset witness here is
an application rule over native segment centers, not a recursive trend proof.

Confirmed third-class departures supply directional facts. An established
opposite, strictly separated center pair supplies a reset witness. Overlap,
repartition, missing history and unfinished predecessors never reset to one.
All facts must have been available at the candidate's own observation clock.
"""
from chanlun.screening.rules import _native_center_body_source_valid

SCHEMA = 'native-first-center-sequence-v2'


class CenterSequences:
    def __init__(self, snapshot, source_valid):
        self.snapshot = snapshot
        self.source_valid = source_valid
        self.units = {u['unit_id']: u for u in snapshot.get('screening_segments', [])}
        self.positions = {u['unit_id']: i for i, u in enumerate(snapshot.get('screening_segments', []))}
        self.centers = snapshot['levels'][0].get('centers', [])
        self.points = snapshot['levels'][0].get('points', [])
        self._bodies = {}

    def _body(self, center, known_at):
        key = center['center_id'], known_at
        if key not in self._bodies:
            self._bodies[key] = _native_center_body_source_valid(
                self.snapshot, center, self.units, self.positions, known_at)
        return self._bodies[key]

    def assess(self, point):
        if point.get('point_type') not in {'3buy', '3sell'}:
            return None
        side = point['side']
        direction = 'up' if side == 'buy' else 'down'
        known = point.get('available_at')
        output = {'schema': SCHEMA, 'scope': 'native_operating_sequence',
                  'direction': direction, 'state': 'unverified', 'ordinal': None,
                  'minimum_ordinal': None, 'center_id': point.get('center_id'),
                  'known_at': known, 'origin_center_id': None, 'origin_witness': None,
                  'symbol': self.snapshot.get('symbol'), 'frequency': self.snapshot.get('source_frequency'),
                  'origin_center_started_at': None,
                  'preceding_center_ids': [], 'reason': 'SEQUENCE_HISTORY_UNVERIFIED'}
        try:
            return self._assess(point, output)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return {**output, 'state': 'unverified', 'ordinal': None, 'reason': 'SEQUENCE_METADATA_INCOMPLETE'}

    def _assess(self, point, output):
        known = output['known_at']
        direction = output['direction']
        opposite = 'down' if direction == 'up' else 'up'
        if (type(known) is not int or known > self.snapshot['source_closed_at']
                or self.snapshot.get('analysis_scope') != 'native_centers'
                or self.source_valid.get(point.get('point_id')) is not True):
            return output
        ids = [c['center_id'] for c in self.centers]
        if len(ids) != len(set(ids)) or point['center_id'] not in ids:
            return output
        # A current snapshot may contain later centers. They must never alter
        # what could be established at this point's own available_at.
        target_index = ids.index(point['center_id'])
        centers = self.centers[:target_index + 1]
        clocks = [c['points'][0]['time'] for c in centers]
        if clocks != sorted(clocks) or len(set(clocks)) != len(clocks):
            return output
        departures = {}
        for candidate in self.points:
            if (candidate.get('point_type') in {'3buy', '3sell'} and candidate.get('status') == 'confirmed'
                    and type(candidate.get('available_at')) is int and candidate['available_at'] <= known
                    and self.source_valid.get(candidate.get('point_id')) is True):
                departures[candidate['center_id']] = candidate
        previous, previous_valid = None, False
        origin_witness = None
        active = []
        uncertain = False
        for center in centers:
            identity = center['center_id']
            valid = self._body(center, known)
            prior = departures.get(previous['center_id']) if previous else None
            prior_direction = ('up' if prior['point_type'] == '3buy' else 'down') if prior else None
            separated_opposite = bool(previous and valid and previous_valid and (
                center['envelope']['gg_tick'] < previous['envelope']['dd_tick'] if direction == 'up'
                else center['envelope']['dd_tick'] > previous['envelope']['gg_tick']))
            if separated_opposite and prior_direction == opposite:
                origin_witness = {'kind': 'opposite_departure_and_separated_centers',
                                  'center_ids': [previous['center_id'], identity],
                                  'departure_point_id': prior['point_id'],
                                  'departure_available_at': prior['available_at']}
                active = []
                uncertain = False
            if not valid:
                # A hole in the causal sequence is not a new trend origin.
                uncertain = True
                previous, previous_valid = center, False
                continue
            is_target = identity == point['center_id']
            event = departures.get(identity)
            observed_direction = ('up' if event['point_type'] == '3buy' else 'down') if event else None
            if is_target or active or observed_direction == direction:
                active.append(identity)
            if not is_target and event is None:
                uncertain = True
            if is_target:
                output.update(origin_center_id=active[0] if active else None,
                              origin_witness=origin_witness,
                              preceding_center_ids=active[:-1])
                if active:
                    output['origin_center_started_at'] = next(c['points'][0]['time'] for c in centers if c['center_id'] == active[0])
                if uncertain:
                    output['reason'] = 'SEQUENCE_CAUSAL_GAP'
                elif len(active) > 1:
                    output.update(state='later', minimum_ordinal=len(active),
                                  ordinal=len(active) if origin_witness else None,
                                  reason='EARLIER_CENTER_IN_OPERATING_SEQUENCE')
                elif origin_witness:
                    output.update(state='first', ordinal=1, minimum_ordinal=1,
                                  reason='VERIFIED_FIRST_AFTER_OPPOSITE_SEQUENCE')
                return output
            previous, previous_valid = center, valid
        return output


def first_verified(sequence):
    if not isinstance(sequence, dict):
        return False
    witness = sequence.get('origin_witness') or {}
    return (sequence.get('schema') == SCHEMA and sequence.get('state') == 'first'
            and type(sequence.get('ordinal')) is int and sequence['ordinal'] == 1
            and isinstance(witness, dict) and witness.get('kind') == 'opposite_departure_and_separated_centers'
            and isinstance(witness.get('center_ids'), list) and len(witness['center_ids']) == 2
            and all(isinstance(value, str) and value for value in witness['center_ids'])
            and witness['center_ids'][0] != witness['center_ids'][1]
            and isinstance(witness.get('departure_point_id'), str) and bool(witness['departure_point_id'])
            and type(witness.get('departure_available_at')) is int and type(sequence.get('known_at')) is int
            and witness['departure_available_at'] <= sequence['known_at']
            and sequence.get('origin_center_id') == sequence.get('center_id')
            and sequence.get('preceding_center_ids') == [])
