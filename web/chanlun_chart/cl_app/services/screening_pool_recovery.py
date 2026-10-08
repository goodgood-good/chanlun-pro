"""Retain prior screening members until failed replacement checks resolve."""

from copy import deepcopy


def symbol_key(row):
    return f"{row.get('market', 'a')}:{row['code']}"


def independent_members(state):
    """Keep saved watchlist/holding membership until those sources can be read."""
    seed = state.get("seed") or {}
    screened = {symbol_key(row) for row in seed.get("screening_symbols", [])}
    held = set(state.get("holding_symbols", []))
    return {
        symbol_key(row): deepcopy(row)
        for row in seed.get("symbols", [])
        if row.get("origin") == "watchlist"
        or symbol_key(row) in held
        or symbol_key(row) not in screened
    }


def pending_members(seed, result, selected_keys):
    previous = {
        symbol_key(row): row for row in (seed or {}).get("screening_symbols", [])
    }
    frequencies = set(result.get("settings", {}).get("frequencies", []))
    failed = {
        symbol_key(error)
        for error in result.get("errors", [])
        if error.get("code")
        and error.get("signal_type") != "consolidation_divergence"
        and error.get("frequency") in frequencies | {None}
    }
    return {
        key: deepcopy(previous[key])
        for key in sorted(failed & previous.keys() - selected_keys)
    }


def suspend_pending(state, pending, previous_checks, stamp):
    for identity, item in list(state["signals"].items()):
        if symbol_key(item) in pending:
            state["unverified_signals"][identity] = state["signals"].pop(identity)
    for symbol in pending:
        for frequency in state["seed"]["settings"].get("frequencies", []):
            key = f"{symbol}:{frequency}"
            old = previous_checks.get(key, {})
            # Preserve the last successful cutoff for regression checks and
            # notification deduplication; it is no longer current proof.
            state["selection_checks"][key] = {
                **deepcopy(old),
                "symbol": symbol,
                "error": True,
                "failed_at": stamp,
                "reason": "SCREENING_RECHECK_PENDING",
            }


def resolve_pending(state, checked, revision):
    if not revision:
        return False
    seed = state.get("seed") or {}
    pending = seed.get("screening_rechecks", {})
    frequencies = seed.get("settings", {}).get("frequencies", [])
    resolved, negative = set(), set()
    for symbol in pending.keys() & checked:
        checks = [
            state["selection_checks"].get(f"{symbol}:{frequency}", {})
            for frequency in frequencies
        ]
        usable = [
            check
            for check in checks
            if check.get("revision") == revision
            and check.get("error") is False
            and type(check.get("cutoff")) is int
            and isinstance(check.get("identities"), list)
        ]
        if any(check["identities"] for check in usable):
            resolved.add(symbol)
        elif checks and len(usable) == len(checks):
            resolved.add(symbol)
            negative.add(symbol)
    if resolved:
        seed["screening_rechecks"] = {
            key: row for key, row in pending.items() if key not in resolved
        }
        seed["screening_symbols"] = [
            row for row in seed["screening_symbols"] if symbol_key(row) not in negative
        ]
    return bool(resolved)
