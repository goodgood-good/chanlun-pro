"""Shared price-boundary predicates, independent of lifecycle and signal models."""


def return_outside_core(
    *, direction: str, low_tick: int, high_tick: int, zd_tick: int, zg_tick: int,
) -> bool:
    """A return must be disjoint from the closed core, including its boundary.

    L054's second decomposition explicitly calls d4g4 an oscillation when
    g4=d2, despite the independent g3d4 departure (D:/缠论/
    chanlun_lesson_corpus/L054_一个具体走势的分析(2007-05-24013731).md:181-196).
    Strict tick inequalities implement that example and its upward mirror;
    they disambiguate L020:427-436's verbal "不跌破/不升破" using the diagram.

    This only checks price geometry. Callers must also prove direction, source
    grade, independent departure, first-return ownership and confirmation time.
    """
    if direction == "up":
        return low_tick > zg_tick
    if direction == "down":
        return high_tick < zd_tick
    raise ValueError("unknown departure direction")
