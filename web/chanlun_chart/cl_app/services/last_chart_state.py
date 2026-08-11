"""用户最后访问的 (market, code, frequency) 三元组持久化（B5 part 1）。

启动期 chart_data 预热依赖这份记录知道用户上次在看什么标的，把对应的
chart_cache_disk entry 提前回填 RAM 热层。

存储：${DATA_PATH}/cache/last_chart_state.json
格式：{ "schema": "current", "updated_at": <ts>, "market", "code", "frequency" }

防抖：tv_history 在 firstDataRequest=true 时调，但 TV widget 切周期/标的可能
触发连续多次 first=true，5s 内同三元组不重复写盘。
"""
import json
import os
import tempfile
import threading
import time

from chanlun import config
from chanlun.tools.log_util import LogUtil

_SCHEMA = "current"
_LOCK = threading.Lock()
_last_record = None  # ((market, code, frequency), wall_time)
_DEBOUNCE_SECONDS = 5.0


def _state_path() -> str:
    base = config.get_data_path()
    return os.path.join(str(base), "cache", "last_chart_state.json")


def record_user_request(market: str, code: str, frequency: str) -> None:
    """记录用户最近访问的三元组。失败仅 warn 不抛。"""
    global _last_record
    key = (market, code, frequency)
    now = time.time()
    # 防抖判断 + _last_record 更新纳入 _LOCK:原在锁外读写,并发(多 tab/切周期)下两线程
    # 同读旧值都判定"需写"→防抖失效 + lost-update(审查 B-3)。锁内 check-then-set 杜绝。
    with _LOCK:
        if (
            _last_record is not None
            and _last_record[0] == key
            and now - _last_record[1] < _DEBOUNCE_SECONDS
        ):
            return
        _last_record = (key, now)

    payload = {
        "schema": _SCHEMA,
        "updated_at": int(now),
        "market": market,
        "code": code,
        "frequency": frequency,
    }
    path = _state_path()
    try:
        with _LOCK:
            dir_ = os.path.dirname(path)
            os.makedirs(dir_, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".tmp",
                dir=dir_,
                delete=False,
            ) as tf:
                json.dump(payload, tf, ensure_ascii=False)
                tmp = tf.name
            os.replace(tmp, path)
    except Exception as e:
        LogUtil.warning(f"[last_chart_state] 写 {path} 失败: {e}")


def load_last_state():
    """启动期读最后一次访问状态。返回 dict 或 None。"""
    path = _state_path()
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if data.get("schema") != _SCHEMA:
            return None
        if not all(data.get(k) for k in ("market", "code", "frequency")):
            return None
        return data
    except Exception as e:
        LogUtil.warning(f"[last_chart_state] 读 {path} 失败: {e}")
        return None
