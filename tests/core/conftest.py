"""让 tests/core 能 import src 下的 chanlun.core。"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
