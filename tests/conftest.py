"""pytest 配置：把 src/ 加入 import 路径，使 `import chanlun` 不依赖安装即可用。"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# 孤儿 chan 测试（指向不存在的 `chan` 包）隔离于此，不参与收集。
# 保留作为未来“干净重写”（spec Option C）的现成规格蓝图。
collect_ignore_glob = ["_spec_chan_clean/*"]
