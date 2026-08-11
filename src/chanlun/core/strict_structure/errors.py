"""严格缠论结构合同失败的明确异常边界。"""


class StrictStructureContractError(RuntimeError):
    """仅在严格证据构建内部遇到无效证据时抛出。"""


__all__ = ("StrictStructureContractError",)
