"""让 tests/web 下的测试能 import web 服务模块(cl_app)与 src(chanlun)。"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))
