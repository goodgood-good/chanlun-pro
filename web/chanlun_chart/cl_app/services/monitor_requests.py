"""Independent owners for durable, one-shot minute verification requests."""
from copy import deepcopy


def sources(value):
    """Read current records and pre-upgrade scalar/screening tokens safely."""
    if isinstance(value, dict) and value.get('schema') == 'minute-requests-v1':
        return deepcopy(value.get('sources', {}))
    if isinstance(value, dict) and value.get('source') == 'screening':
        return {f"screening:{value.get('run_id')}": deepcopy(value)}
    return {} if value is None else {'manual': deepcopy(value)}


def snapshot(state, key):
    return {'schema': 'minute-requests-v1',
            'sources': sources(state.get('minute_force_pending', {}).get(key))}


def next_manual_generation(state):
    values = [state.get('minute_request_generation', 0)]
    for name in ('minute_force_pending', 'active_minute_override_tokens'):
        values.extend(sources(value).get('manual', 0) for value in state.get(name, {}).values())
    generation = max([value for value in values if type(value) is int] or [0]) + 1
    state['minute_request_generation'] = generation
    return generation


def add(state, key, owner, generation):
    pending = state.setdefault('minute_force_pending', {})
    current = sources(pending.get(key))
    old = current.get(owner)
    if owner == 'manual' and type(old) is int and type(generation) is int and old > generation:
        return
    current[owner] = deepcopy(generation)
    pending[key] = {'schema': 'minute-requests-v1', 'sources': current}


def acknowledge(state, key, dispatched):
    pending = state.get('minute_force_pending', {})
    current = sources(pending.get(key))
    for owner, generation in sources(dispatched).items():
        if current.get(owner) == generation:
            current.pop(owner, None)
    if current:
        pending[key] = {'schema': 'minute-requests-v1', 'sources': current}
    else:
        pending.pop(key, None)


def discard_screening(state, run_id):
    pending = state.get('minute_force_pending', {})
    owner = f'screening:{run_id}'
    changed = False
    for key, value in list(pending.items()):
        current = sources(value)
        if owner not in current:
            continue
        current.pop(owner)
        changed = True
        if current:
            pending[key] = {'schema': 'minute-requests-v1', 'sources': current}
        else:
            pending.pop(key, None)
    return changed
