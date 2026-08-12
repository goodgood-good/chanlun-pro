"""QMT 行情下载与读取使用的统一时间边界契约。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def qmt_exclusive_download_end(inclusive_end: datetime | str) -> str:
    """把业务侧包含端点的时刻转换为 QMT 下载接口的不包含端点。"""

    boundary = pd.Timestamp(inclusive_end)
    if pd.isna(boundary):
        raise ValueError("QMT 下载结束时刻无效")
    if boundary.tzinfo is not None:
        boundary = boundary.tz_convert(_SHANGHAI).tz_localize(None)
    return (boundary + pd.Timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")


__all__ = ("qmt_exclusive_download_end",)
