"""Explicit exception boundary for strict Chan-structure contract failures."""


class StrictStructureContractError(RuntimeError):
    """Raised only inside strict evidence construction for invalid evidence."""


__all__ = ("StrictStructureContractError",)
