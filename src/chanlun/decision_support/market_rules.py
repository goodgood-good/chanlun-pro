from __future__ import annotations

from decimal import Decimal
import re


_CODE_RE = re.compile(r"^(SH|SZ|BJ)\.(\d{6})$")
_ST_NAME_RE = re.compile(r"^(?:\*?ST|S\*ST)(?![A-Z])", re.IGNORECASE)
_BOARD_LIMITS = {
    "main": Decimal("0.10"),
    "st": Decimal("0.05"),
    "main_st": Decimal("0.05"),
    "gem": Decimal("0.20"),
    "star": Decimal("0.20"),
    "bj": Decimal("0.30"),
}


def is_st_name(name: str) -> bool:
    return bool(_ST_NAME_RE.match(name.strip()))


def a_share_base_board(code: str) -> str | None:
    match = _CODE_RE.fullmatch(code.strip().upper())
    if match is None:
        return None
    exchange, number = match.groups()
    if exchange == "BJ" and number.startswith(("4", "8", "920")):
        return "bj"
    if exchange == "SH":
        if number.startswith(("688", "689")):
            return "star"
        if number.startswith(("600", "601", "603", "605")):
            return "main"
    if exchange == "SZ":
        if number.startswith(("300", "301")):
            return "gem"
        if number.startswith(("000", "001", "002", "003")):
            return "main"
    return None


def a_share_board(code: str, name: str) -> str | None:
    board = a_share_base_board(code)
    if board == "main" and is_st_name(name):
        return "main_st"
    return board


def a_share_limit_pct(board: str) -> Decimal | None:
    return _BOARD_LIMITS.get(board.strip().casefold())
