"""Single active trading-system implementation.

Import concrete APIs from their defining modules.  Keeping this package
initializer side-effect free prevents one service import from loading every
research, replay, and execution module.
"""
