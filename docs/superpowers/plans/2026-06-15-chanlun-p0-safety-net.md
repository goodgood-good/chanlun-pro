# Phase 0 安全网(chanlun.core 黄金主测试)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为生产核心 `chanlun.core.CL` 建立确定性"黄金主"回归测试,隔离孤儿 `chan` 测试,让 `pytest tests/` 变绿且能在后续优化中抓住任何结构漂移。

**Architecture:** 把固定窗口的真实 K 线冻结成 parquet(`tests/fixtures/`);用生产同款 `CL(code, freq, dict(CL_CFG))` 重算,把 笔/段/中枢/买卖点/背驰 经 `to_dict()` 规范化成 JSON 快照,与提交的 golden(`tests/golden/`)逐字符比对。生成与断言共用一份快照逻辑(`tests/chan_core/snapshot.py`),确保口径一致。

**Tech Stack:** Python 3.11、pytest、pandas/pyarrow(parquet)、`chanlun.core.cl.CL`、`chanlun.recursive_bt.engine.CL_CFG`、`chanlun.exchange.get_exchange`(QMT/cq)。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `tests/conftest.py`(改) | 追加 `collect_ignore_glob` 忽略隔离后的孤儿目录 |
| `tests/_spec_chan_clean/`(由 `tests/chan/` 移入) | 孤儿"干净重写"测试,隔离保留作未来蓝图 |
| `tests/_spec_chan_clean/README.md`(新) | 说明它是孤儿规格、为何隔离 |
| `tests/chan_core/snapshot.py`(新) | `cl_snapshot()` + `canonical_json()`:生成与断言共用 |
| `tests/chan_core/test_snapshot.py`(新) | 快照工具的单元测试(浮点定点/键排序) |
| `tests/chan_core/test_golden_master.py`(新) | 黄金主回归 + 变异检验 |
| `tests/tools/gen_fixtures.py`(新) | 一次性:拉真实K线→parquet;由 parquet→golden |
| `tests/fixtures/*.parquet`(生成) | 冻结的确定性输入 |
| `tests/golden/*.json`(生成) | 冻结的期望快照 |
| `tests/README.md`(新) | 平台相关性、`--update-golden` 流程、孤儿定位 |

> **导入约定**:不建 `__init__.py`;测试与脚本用 2 行 `sys.path.insert(parent)` + `from snapshot import ...` 的兄弟导入(对 pytest prepend / importlib 两种模式都稳)。

---

## Task 1: 隔离孤儿测试 + 绿色基线

**Files:**
- Move: `tests/chan/` → `tests/_spec_chan_clean/`
- Create: `tests/_spec_chan_clean/README.md`
- Modify: `tests/conftest.py`

- [ ] **Step 1: 移动孤儿目录(它是 untracked,直接移)**

Run:
```bash
git -C D:/project/chanlun-pro mv tests/chan tests/_spec_chan_clean 2>/dev/null || mv tests/chan tests/_spec_chan_clean
```
Expected: `tests/chan/` 不再存在,`tests/_spec_chan_clean/test_*.py` 就位。

- [ ] **Step 2: 让 pytest 忽略隔离目录**

在 `tests/conftest.py` 末尾追加(保留原有 `src` 路径插入不动):
```python

# 孤儿 chan 测试(指向不存在的 `chan` 包)隔离于此,不参与收集。
# 保留作为未来"干净重写"(spec Option C)的现成规格蓝图。
collect_ignore_glob = ["_spec_chan_clean/*"]
```

- [ ] **Step 3: 写隔离说明**

Create `tests/_spec_chan_clean/README.md`:
```markdown
# 孤儿测试:干净 `chan` 包的规格蓝图(已隔离)

这些测试 `import chan`(一个**不存在**的干净重写包),对应实现 `src/chan` 在 git
全历史中从未存在,`tests/fixtures` 原本也为空。它们不测当前生产核心
`chanlun.core`,且会让 `pytest` collection-error,故隔离于此(`conftest.py`
的 `collect_ignore_glob` 跳过本目录)。

**保留原因**:它们是一份写得很讲究(带課次断言、`Duan` dataclass、
`find_zhongshus(segs)` 等干净 API)的规格,是将来若决定做"干净重写"
(设计 spec 的 Option C)的现成蓝图。**勿删**。

生产核心的回归保护见 `tests/chan_core/`(黄金主测试)。
```

- [ ] **Step 4: 验证 pytest 不再 collection-error**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -8`
Expected: **无 `ERROR` / `errors during collection`**;出现 `no tests ran`(因 chan_core 还没建)属正常。

- [ ] **Step 5: Commit**

```bash
cd /d/project/chanlun-pro
git add tests/conftest.py tests/_spec_chan_clean
git commit -m "test(chan_core): 隔离孤儿 chan 测试——消除 pytest collection-error

tests/chan 全部 import 不存在的 chan 包(src/chan 从未提交、fixtures 空),
集体 collection-error 致生产核心零回归保护。移至 tests/_spec_chan_clean 并
collect_ignore_glob 跳过,保留作未来干净重写蓝图。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 共用快照工具(TDD)

**Files:**
- Create: `tests/chan_core/snapshot.py`
- Test: `tests/chan_core/test_snapshot.py`

- [ ] **Step 1: 写失败测试**

Create `tests/chan_core/test_snapshot.py`:
```python
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from snapshot import _round_floats, canonical_json


def test_round_floats_nested():
    obj = {"b": 1.123456789, "a": [2.987654321, {"c": 3.0000000004}]}
    assert _round_floats(obj, 8) == {"b": 1.12345679, "a": [2.98765432, {"c": 3.0}]}


def test_canonical_json_sorts_keys_and_rounds():
    s = canonical_json({"b": 1.0, "a": 2.123456789}, 8)
    assert s == '{\n  "a": 2.12345679,\n  "b": 1.0\n}'
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe -m pytest tests/chan_core/test_snapshot.py -q`
Expected: FAIL(`ModuleNotFoundError: No module named 'snapshot'`)

- [ ] **Step 3: 写实现**

Create `tests/chan_core/snapshot.py`:
```python
# -*- coding: utf-8 -*-
"""生产核心 chanlun.core.CL 的结构化快照 + 规范化序列化。

gen_fixtures(生成 golden)与 test_golden_master(断言)共用,确保口径一致。
"""
import json
from typing import Any


def _round_floats(obj: Any, ndigits: int = 8) -> Any:
    """递归把所有 float 定点到 ndigits 位,压掉跨平台 1-ulp 噪声。"""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def cl_snapshot(cd) -> dict:
    """把 CL 的全结构输出序列化为可比较 dict。

    买卖点(mmds)与背驰(bcs)已内嵌在 BI/XD.to_dict() 中,无需单列。
    """
    return {
        "code": cd.code,
        "frequency": cd.frequency,
        "kline_num": len(cd.get_klines()),
        "bis": [b.to_dict() for b in cd.get_bis()],
        "xds": [x.to_dict() for x in cd.get_xds()],
        "bi_zss": [z.to_dict() for z in cd.get_bi_zss()],
        "xd_zss": [z.to_dict() for z in cd.get_xd_zss()],
    }


def canonical_json(obj: Any, ndigits: int = 8) -> str:
    """规范化:浮点定点 + 键排序 + UTF-8。消除键序/平台浮点噪声。"""
    return json.dumps(
        _round_floats(obj, ndigits), sort_keys=True, ensure_ascii=False, indent=2
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe -m pytest tests/chan_core/test_snapshot.py -q`
Expected: PASS(2 passed)

- [ ] **Step 5: Commit**

```bash
cd /d/project/chanlun-pro
git add tests/chan_core/snapshot.py tests/chan_core/test_snapshot.py
git commit -m "test(chan_core): 黄金主快照工具(规范化序列化,生成/断言共用)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: fixture 生成器 + 拉真实数据

> ⚠️ **环境门控**:`--pull` 需本地 QMT(A股)+ cq/长桥(美股)可用(用户实盘在用)。
> 历史数据不可变 → 同窗口重拉得到同 parquet。`--pull` 由有数据访问权的一方运行
> (可能是用户机器);`--update-golden` 与测试仅读本地 parquet、不触网。

**Files:**
- Create: `tests/tools/gen_fixtures.py`

- [ ] **Step 1: 写生成器**

Create `tests/tools/gen_fixtures.py`:
```python
# -*- coding: utf-8 -*-
"""一次性 fixture 生成器。

  python tests/tools/gen_fixtures.py --pull           # 触网拉数据(需 QMT+cq),覆盖 parquet
  python tests/tools/gen_fixtures.py --update-golden   # 离线:由已提交 parquet 重生 golden
  python tests/tools/gen_fixtures.py --pull --update-golden

固定窗口 + 拉后按 [start,end] 闭区间裁剪 → 跨适配器口径一致、可复现。
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src" if (_HERE.parents[2] / "src").exists() else _HERE.parents[2] / "src"))
sys.path.insert(0, str(_HERE.parents[1] / "chan_core"))

from chanlun.base import Market                       # noqa: E402
from chanlun.exchange import get_exchange             # noqa: E402
from chanlun.core.cl import CL                        # noqa: E402
from chanlun.recursive_bt.engine import CL_CFG        # noqa: E402
from snapshot import cl_snapshot, canonical_json      # noqa: E402

FIX_DIR = _HERE.parents[1] / "fixtures"
GOLD_DIR = _HERE.parents[1] / "golden"

# 固定窗口(可复现)。code 为项目内部格式;若适配器拒绝,查 QMT.code_to_qmt /
# cq._market_of_code 调整。
FIXTURES = [
    {"key": "SH.600519_d",   "market": Market.A,  "code": "SH.600519", "freq": "d",   "start": "2023-01-01", "end": "2024-06-30"},
    {"key": "SH.600519_30m", "market": Market.A,  "code": "SH.600519", "freq": "30m", "start": "2024-03-01", "end": "2024-06-30"},
    {"key": "QQQ_d",         "market": Market.US, "code": "QQQ",        "freq": "d",   "start": "2023-01-01", "end": "2024-06-30"},
    {"key": "QQQ_30m",       "market": Market.US, "code": "QQQ",        "freq": "30m", "start": "2024-03-01", "end": "2024-06-30"},
]


def _pull_one(f) -> pd.DataFrame:
    ex = get_exchange(f["market"])
    df = ex.klines(f["code"], f["freq"], start_date=f["start"], end_date=f["end"])
    if df is None or len(df) == 0:
        raise RuntimeError(f"{f['key']}: 取数为空,检查 QMT/cq 是否就绪、code 格式")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    mask = (df["date"] >= pd.Timestamp(f["start"])) & (df["date"] <= pd.Timestamp(f["end"]) + pd.Timedelta(days=1))
    df = df.loc[mask].reset_index(drop=True)
    cols = [c for c in ("code", "date", "open", "high", "low", "close", "volume") if c in df.columns]
    return df[cols]


def pull():
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    for f in FIXTURES:
        df = _pull_one(f)
        df.to_parquet(FIX_DIR / f"{f['key']}.parquet", index=False)
        print(f"[pull] {f['key']}: {len(df)} bars -> {f['key']}.parquet")


def update_golden():
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    for f in FIXTURES:
        p = FIX_DIR / f"{f['key']}.parquet"
        if not p.exists():
            print(f"[golden] 跳过 {f['key']}(无 parquet,先 --pull)")
            continue
        df = pd.read_parquet(p)
        cd = CL(f["code"], f["freq"], dict(CL_CFG))
        cd.process_klines(df)
        (GOLD_DIR / f"{f['key']}.json").write_text(canonical_json(cl_snapshot(cd)), encoding="utf-8")
        print(f"[golden] {f['key']}: bis={len(cd.get_bis())} xds={len(cd.get_xds())} -> {f['key']}.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--update-golden", action="store_true")
    a = ap.parse_args()
    if not (a.pull or a.update_golden):
        ap.error("至少指定 --pull 或 --update-golden")
    if a.pull:
        pull()
    if a.update_golden:
        update_golden()
```

- [ ] **Step 2: 拉真实数据(环境门控)**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe tests/tools/gen_fixtures.py --pull`
Expected: 打印 4 行 `[pull] ... N bars`;`tests/fixtures/` 出现 4 个 `.parquet`。
> 若报取数为空/超时:确认本机 QMT 已启动、cq(长桥)key 已配;A 股 code 被拒则查 `ExchangeQMT.code_to_qmt`、美股查 `ExchangeChangQiao` 的 code 格式。此步若本会话环境无数据访问,交由用户在实盘机执行一次后把 parquet 回传。

- [ ] **Step 3: 核验 parquet 合理**

Run:
```bash
cd /d/project/chanlun-pro && .venv/Scripts/python.exe -c "import pandas as pd, glob; [print(p.split('/')[-1], len(pd.read_parquet(p)), list(pd.read_parquet(p).columns)) for p in glob.glob('tests/fixtures/*.parquet')]"
```
Expected: 每个文件 ~100–500 行,列含 `date,open,high,low,close`。

- [ ] **Step 4: Commit(parquet 一并冻结提交)**

```bash
cd /d/project/chanlun-pro
git add tests/tools/gen_fixtures.py tests/fixtures
git commit -m "test(chan_core): fixture 生成器 + 冻结真实K线 parquet(QMT 茅台 / cq QQQ)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 黄金主回归测试 + 变异检验

**Files:**
- Create: `tests/chan_core/test_golden_master.py`
- Generate: `tests/golden/*.json`

- [ ] **Step 1: 写测试(此刻 golden 还没生成 → 预期失败)**

Create `tests/chan_core/test_golden_master.py`:
```python
# -*- coding: utf-8 -*-
"""黄金主回归:钉住生产核心 chanlun.core.CL 当前结构化行为。

任何后续优化若改变 笔/段/中枢/买卖点/背驰 输出,这里立即报警。
golden 是平台相关的(当前 Windows + numpy/scipy 版本);依赖大版本变动后
用 `gen_fixtures.py --update-golden` 重生并人工复核。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from snapshot import cl_snapshot, canonical_json  # noqa: E402
from chanlun.core.cl import CL                     # noqa: E402
from chanlun.recursive_bt.engine import CL_CFG     # noqa: E402

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures"
GOLD_DIR = Path(__file__).resolve().parents[1] / "golden"


def _keys():
    return sorted(p.stem for p in FIX_DIR.glob("*.parquet"))


def _build(key: str) -> CL:
    df = pd.read_parquet(FIX_DIR / f"{key}.parquet")
    code, freq = key.rsplit("_", 1)
    cd = CL(code, freq, dict(CL_CFG))
    cd.process_klines(df)
    return cd


@pytest.mark.parametrize("key", _keys())
def test_golden_master(key):
    got = canonical_json(cl_snapshot(_build(key)))
    golden = (GOLD_DIR / f"{key}.json").read_text(encoding="utf-8")
    assert got == golden, f"{key} 结构化输出相对 golden 漂移(疑似回归)"


def test_drift_is_detected():
    """变异检验:证明安全网真会响。整体缩放价格 → 快照价格字段必变。"""
    keys = _keys()
    assert keys, "无 fixture,先跑 gen_fixtures.py --pull"
    key = keys[0]
    code, freq = key.rsplit("_", 1)
    base = canonical_json(cl_snapshot(_build(key)))
    df = pd.read_parquet(FIX_DIR / f"{key}.parquet").copy()
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * 1.05  # 哨兵扰动:单调变换保结构,但所有价格字段位移
    cd = CL(code, freq, dict(CL_CFG))
    cd.process_klines(df)
    assert canonical_json(cl_snapshot(cd)) != base, "扰动价格后快照未变 → 安全网失效"
```

- [ ] **Step 2: 跑测试确认失败(golden 缺失)**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe -m pytest tests/chan_core/test_golden_master.py -q`
Expected: `test_golden_master[*]` FAIL(`FileNotFoundError` golden);`test_drift_is_detected` PASS。

- [ ] **Step 3: 生成 golden**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe tests/tools/gen_fixtures.py --update-golden`
Expected: 打印 4 行 `[golden] ... bis=.. xds=..`;`tests/golden/` 出现 4 个 `.json`。

- [ ] **Step 4: 人工抽查 golden 合理性**

Run:
```bash
cd /d/project/chanlun-pro && .venv/Scripts/python.exe -c "import json,glob; [print(p.split('/')[-1], 'bis=%d xds=%d bi_zss=%d xd_zss=%d kn=%d'%(len(d['bis']),len(d['xds']),len(d['bi_zss']),len(d['xd_zss']),d['kline_num'])) for p in glob.glob('tests/golden/*.json') for d in [json.load(open(p,encoding='utf-8'))]]"
```
Expected: 每个标的 bis/xds 非 0(日线几十~上百笔),数值不空。**人工确认非全 0、非异常**。

- [ ] **Step 5: 跑测试确认全过**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe -m pytest tests/chan_core/test_golden_master.py -q`
Expected: PASS(4 golden + 1 drift = 5 passed)

- [ ] **Step 6: 二次复现验证(确定性)**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe tests/tools/gen_fixtures.py --update-golden && git status --porcelain tests/golden`
Expected: `git status` **无输出**(golden 逐字节可复现,无改动)。

- [ ] **Step 7: Commit**

```bash
cd /d/project/chanlun-pro
git add tests/chan_core/test_golden_master.py tests/golden
git commit -m "test(chan_core): 黄金主回归 + 变异检验——生产核心首个回归安全网

钉住 chanlun.core.CL 在真实 fixture 上的 笔/段/中枢/买卖点/背驰 全结构;
变异检验证明扰动可被捕获。后续 P1-P4 优化以此零漂移为门控。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: README + 项目记忆 + 全量绿

**Files:**
- Create: `tests/README.md`
- Modify: 项目记忆(MEMORY.md + 相关条目)

- [ ] **Step 1: 写 tests/README.md**

Create `tests/README.md`:
```markdown
# tests/ —— 生产核心回归安全网

## 布局
- `chan_core/` —— 生产核心 `chanlun.core.CL` 的**黄金主**回归测试(真实数据驱动)
- `fixtures/*.parquet` —— 冻结的确定性 K 线(QMT 茅台 / cq QQQ,固定窗口)
- `golden/*.json` —— 冻结的期望结构化快照
- `tools/gen_fixtures.py` —— 一次性生成器(`--pull` 触网 / `--update-golden` 离线)
- `_spec_chan_clean/` —— **孤儿**测试(干净重写蓝图,已 collect_ignore,勿删)

## 跑测试
    .venv/Scripts/python.exe -m pytest tests/ -q

## golden 漂移了怎么办
黄金主测试失败 = 某改动改变了 笔/段/中枢/买卖点/背驰 输出。
- 若是**预期内**的行为变更:`gen_fixtures.py --update-golden` 重生 golden,
  **人工 diff 复核**确认变更符合预期,再提交。
- 若是**意外回归**:别动 golden,去查改动。

## 注意
- golden **平台相关**(当前 Windows + numpy/scipy 版本)。换平台/依赖大版本需重生。
- fixture 是 **parquet 不是 csv**:笔对 ~4e-16 浮点噪声敏感,csv 往返丢精度会改结构。
- `--pull` 需本地 QMT(A股)+ cq/长桥(美股);拉一次冻结后,日常测试只读 parquet、不触网。
```

- [ ] **Step 2: 全量测试确认绿**

Run: `cd /d/project/chanlun-pro && .venv/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -8`
Expected: 全 PASS(chan_core 下 5 passed),无 ERROR,孤儿目录被忽略。

- [ ] **Step 3: 更新项目记忆**

更新 `C:/Users/lc/.claude/projects/D--project-chanlun-pro/memory/`:
- 在 `project_recursive_trading_backtest.md` 修正"910 passed"为过时,记录:当前安全网=`tests/chan_core` 黄金主(QMT茅台/cq QQQ 4 fixture),孤儿 `chan` 测试已隔离 `tests/_spec_chan_clean`。
- 新增/更新一条 `project` 记忆:`src/chanlun` 整体优化路线图 P0→P4(spec 见 `docs/superpowers/specs/2026-06-15-chanlun-optimization-design.md`),P0 已落地。
- 在 `MEMORY.md` 加一行指针。

- [ ] **Step 4: Commit**

```bash
cd /d/project/chanlun-pro
git add tests/README.md
git commit -m "docs(tests): 安全网 README(golden 漂移处置/平台相关/parquet 铁律)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 0 Done 校验
- [ ] `pytest tests/` 无 collection-error;`tests/chan_core` 5 passed
- [ ] `tests/fixtures/` 4 parquet、`tests/golden/` 4 json 均已提交
- [ ] 变异检验 `test_drift_is_detected` 通过(证明能抓漂移)
- [ ] `gen_fixtures.py --update-golden` 二次运行零 diff(确定性)
- [ ] README + 项目记忆已更新(含纠正"910 passed")
- [ ] 孤儿测试隔离于 `tests/_spec_chan_clean` 且保留

## 自查(spec 覆盖)
- spec §4.2 隔离孤儿 → Task 1 ✓
- spec §4.2 共用快照逻辑 → Task 2 ✓
- spec §4.4 真实 parquet fixture(QMT/cq)→ Task 3 ✓
- spec §4.1/§4.3 黄金主 + CL/CL_CFG 契约 → Task 4 ✓
- spec §4.5 确定性(定点/键排序/复现)→ Task 2 + Task 4 Step 6 ✓
- spec §4.6 变异检验/速度/复现 → Task 4 ✓
- spec §6 README + 记忆更新 → Task 5 ✓
```
