"""已核对的旧笔规则；不在这里附加端点路径评分或完成判据。

原文位置（均在 D:\\缠论\\chanlun_lesson_corpus）：
- L062_分型、笔与线段(2007-06-30094951).md，40–61、64–118 行：
  包含处理后的三 K 分型、相邻顶底及独立 K 线。
- L077_一些概念的再分辨(2007-09-05232401).md，139–148、247–283 行：
  顶底价格关系，以及相邻同类分型中更极端者的取舍。
- L066_主力资金的食物链(2007-07-30224205).md，256–280 行：
  作者 2007-07-31 16:14:47 回复中的笔内最高最低要求（不是正文）。

中心差至少4是独立K线的坐标表达；中心区间的对称价格条件是工程
形式化。全区间极值谓词保留作旧模型审计，未作为当前路径乙的硬门槛。
局部合格不决定整条路径的取舍，也不直接赋予完成状态。
"""

from chanlun.core.types import FX, CLKline


def fractal_kind(left: CLKline, middle: CLKline, right: CLKline):
    """识别包含处理后的三 K 分型，不决定是否作为笔端点。"""
    if getattr(middle, "initial_context_alternatives", ()):
        return None
    alternatives = getattr(left, "initial_context_alternatives", ())
    if alternatives:
        # 缺失前情的工程处理：每个可能左肩都须满足 L062 的同一分型
        # 定义；既不任取初始方向，也不遗漏两种方向都确认的首个分型。
        kinds = {fractal_kind(candidate, middle, right) for candidate in alternatives}
        return next(iter(kinds)) if len(kinds) == 1 else None
    if middle.h > left.h and middle.h > right.h and middle.l > left.l and middle.l > right.l:
        return "ding"
    if middle.l < left.l and middle.l < right.l and middle.h < left.h and middle.h < right.h:
        return "di"
    return None


def is_strictly_more_extreme(later: FX, earlier: FX) -> bool:
    """只比较同类分型的严格极值；相等不会触发替换。

    调用方仍须核对两者之间是否有合格反向结构，不能用价格比较跳笔。
    """
    if later.type != earlier.type:
        return False
    return later.val > earlier.val if later.type == "ding" else later.val < earlier.val


def old_pair_geometry_valid(first: FX, second: FX) -> bool:
    """旧笔间隔及中心区间的对称形式化，不是作者给出的布尔公式。

    L077 139–148 的区间表述用于顶、底；同时要求上下价格翻转得到同一
    资格结果。因此高、低两个界均严格同向，包含或相等边界不放行。
    """
    if first.type == second.type or second.k.index - first.k.index < 4:
        return False
    top, bottom = (first, second) if first.type == "ding" else (second, first)
    return top.k.h > bottom.k.h and top.k.l > bottom.k.l


def old_pair_valid(first: FX, second: FX, interval_extremes) -> bool:
    """L066全区间极值解释的研究审计谓词，不再用于生产路径乙的否决。

    interval_extremes 是中心闭区间的 (最高价, 最低价)。证据缺失时返回
    False；区间内与端点相等的价格允许存在。
    """
    if not old_pair_geometry_valid(first, second) or interval_extremes is None:
        return False
    top, bottom = (first, second) if first.type == "ding" else (second, first)
    return interval_extremes == (top.val, bottom.val)
