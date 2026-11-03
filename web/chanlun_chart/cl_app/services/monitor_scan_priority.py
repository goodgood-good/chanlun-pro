"""Let an active full scan cover expired ordinary closed-market backlog."""
import math
from chanlun.screening.timeframe_policy import POLICY
from .notification_freshness import NOTIFICATION_LIFETIME
from .monitor_requests import sources

PROTECTED = frozenset({'holding','watchlist','independent_member','automatic_membership_unproven',
                      'active_risk_plan','protection_metadata_unverified','pending_minute_notification'})


def defer_to_screening(state, manual, symbol, cutoff, now, revision, scope, *, urgent=False):
    settings=manual.get('settings') or {}
    key=f"{symbol.get('market','a')}:{symbol['code']}"
    if (not revision or manual.get('status') not in {'starting','running'} or manual.get('source_current') is not True
            or manual.get('source_revision')!=revision
            or settings.get('scope') not in {'all_a','all_a_watchlist'}
            or settings.get('timeframe_policy')!=POLICY
            or settings.get('alert_mode')!='native_center_signals'
            or not {'5m','30m'}.issubset(settings.get('frequencies',[]))
            or symbol.get('market','a')!='a' or key not in scope.get('automatic',set())
            or scope.get('protected',{}).get(key) in PROTECTED
            or state.get('force') or urgent
            or type(cutoff) not in (int,float) or not math.isfinite(cutoff) or cutoff<0
            or not now-cutoff>NOTIFICATION_LIFETIME):
        return False
    owners=sources(state.get('minute_force_pending',{}).get(key))
    # Automatic screening handoff requests can wait for that scan. A manual
    # request, or an older request whose owner is unknown, retains precedence.
    if not isinstance(owners,dict) or any(not isinstance(owner,str) or not owner.startswith('screening:') for owner in owners):
        return False
    if key in state.get('force_pending_symbols',[]) and not owners:
        return False
    return True
