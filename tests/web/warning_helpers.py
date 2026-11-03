"""Explicit negative-proof stub for monitor scheduling/notification unit tests.

Frozen-data assessment itself is exercised in test_warning_lifecycle.py.
"""
from chanlun.core.strict_structure.warning_lifecycle import SCHEMA, REASONS


def refute_warning(monkeypatch, reason='STRUCTURE_REPLACED'):
    def checked(_previous, period, _run, _root, _cache):
        return {'schema':SCHEMA, 'state':'invalidated', 'reason_code':reason,
                'reason':REASONS[reason], 'checked_at':period['source_closed_at']}, None
    monkeypatch.setattr('cl_app.services.warning_lifecycle.recheck_warning', checked)
