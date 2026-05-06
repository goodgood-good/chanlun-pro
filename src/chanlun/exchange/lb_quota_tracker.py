"""长桥 history kline 月度配额追踪器。

落盘到 ``~/<chanlun_pro_data>/lb_quota_<YYYY-MM>.json``：

    {
      "month": "2026-05",
      "symbols": ["QQQ.US", "AAPL.US", ...],
      "exhausted": false
    }

每月一个新文件；月切换不读上月数据。线程安全（threading.Lock）。
"""
import datetime
import json
import pathlib
import threading
from typing import Optional, Set

from chanlun import config


class LbQuotaTracker:
    """月度配额追踪器单例。"""

    _instance: "Optional[LbQuotaTracker]" = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "LbQuotaTracker":
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = LbQuotaTracker()
        return cls._instance

    @classmethod
    def _reset_singleton_for_test(cls) -> None:
        """仅用于测试隔离，生产代码不要调。"""
        with cls._instance_lock:
            cls._instance = None

    def __init__(self):
        self._lock = threading.Lock()
        self._symbols: Set[str] = set()
        self._exhausted: bool = False
        self._month: str = self._current_month_key()
        self._load_from_disk()

    @staticmethod
    def _current_month_key() -> str:
        today = datetime.date.today()
        return f"{today.year:04d}-{today.month:02d}"

    def _file_path(self) -> pathlib.Path:
        data_dir = pathlib.Path(config.get_data_path())
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / f"lb_quota_{self._month}.json"

    def _load_from_disk(self) -> None:
        path = self._file_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("month") == self._month:
                self._symbols = set(data.get("symbols", []))
                self._exhausted = bool(data.get("exhausted", False))
        except (OSError, ValueError, json.JSONDecodeError):
            # 文件损坏 / 权限问题：从空集开始，不阻断
            pass

    def _save_to_disk(self) -> None:
        path = self._file_path()
        try:
            path.write_text(
                json.dumps(
                    {
                        "month": self._month,
                        "symbols": sorted(self._symbols),
                        "exhausted": self._exhausted,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            # 落盘失败不阻断主流程，下次再写
            pass

    def _check_month_rollover(self) -> None:
        current = self._current_month_key()
        if current != self._month:
            self._month = current
            self._symbols = set()
            self._exhausted = False
            self._load_from_disk()

    def add_symbol(self, symbol: str) -> None:
        """记录一个成功的 history kline 调用 symbol。"""
        with self._lock:
            self._check_month_rollover()
            if symbol not in self._symbols:
                self._symbols.add(symbol)
                self._save_to_disk()

    def has_symbol(self, symbol: str) -> bool:
        with self._lock:
            self._check_month_rollover()
            return symbol in self._symbols

    def count(self) -> int:
        with self._lock:
            self._check_month_rollover()
            return len(self._symbols)

    def is_exhausted(self, limit: int) -> bool:
        """配额耗尽 = 主动标记过 OR count >= limit。limit=0 视为不检查。"""
        with self._lock:
            self._check_month_rollover()
            if self._exhausted:
                return True
            if limit <= 0:
                return False
            return len(self._symbols) >= limit

    def mark_exhausted(self) -> None:
        """301607 触发时调用，剩余调用立刻短路不再打长桥。"""
        with self._lock:
            self._check_month_rollover()
            if not self._exhausted:
                self._exhausted = True
                self._save_to_disk()
