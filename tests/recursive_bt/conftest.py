"""让 tests/recursive_bt 下的测试能 import src(chanlun)。

仿 tests/web/conftest.py:用 sys.path.insert 注入 src 与 web 路径,
避免依赖外部 PYTHONPATH。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
for _p in (_root / "src", _root / "web" / "chanlun_chart"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
