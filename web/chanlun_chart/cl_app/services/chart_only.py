"""Keep chart viewing independent from background jobs."""
from collections.abc import MutableMapping


def apply_chart_only_mode(settings: MutableMapping) -> None:
    if settings.get("CHART_ONLY_MODE", False):
        settings["SCHEDULER_ENABLED"] = False
