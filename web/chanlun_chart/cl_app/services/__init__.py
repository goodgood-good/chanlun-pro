"""
Service package for cl_app.

Houses shared constants, helpers, and integrations used by blueprints
and the app factory. Designed to reduce duplication and coupling in
route implementations.
"""

__all__ = [
    "chart_cache",
    "chart_compute",
    "constants",
    "stock_list",
    "user_activity",
]
