"""让 tests/exchange 能 import src 下的 chanlun.exchange 与 vendored xtquant。"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
