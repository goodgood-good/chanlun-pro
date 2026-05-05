"""测试 bootstrap：

1. 把项目源码目录加进 sys.path，让相对 import 能解析（src + web/chanlun_chart）。
2. 在 import 时把 chanlun.config.get_data_path 重定向到临时目录，
   避免 web blueprints 模块加载（如 PrewarmManager 单例 __init__）触碰用户真实数据目录。
3. 单独的 fixture（如 manager）会再用 pytest tmp_path 做 per-test 隔离。
"""
import pathlib
import sys
import tempfile

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "chanlun_chart"))

# 模块加载期 fallback 数据目录（避免触碰真实 .chanlun_pro）
_BOOTSTRAP_DATA_DIR = pathlib.Path(
    tempfile.mkdtemp(prefix="chanlun_test_bootstrap_")
)

import chanlun.config as _cfg  # noqa: E402

_cfg.get_data_path = lambda: _BOOTSTRAP_DATA_DIR
