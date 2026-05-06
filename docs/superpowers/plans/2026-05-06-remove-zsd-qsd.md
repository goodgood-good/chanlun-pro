# 删除走势段 (zsd) 与趋势段 (qsd) — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把走势段 (zsd) 与趋势段 (qsd) 两个级别从代码、配置、UI、策略、文档、notebook 中彻底移除，留下干净的 `bi / xd` 两级结构。

**Architecture:** 自下而上分层删除：核心层 (cl_interface/cl/bs_point_calculator) → 工具层 (cl_utils/file_db/kcharts) → 策略层 → Web/UI/脚本 → bundle.js 手工 patch → 文档/notebook。`Config.ZSD_BZH_*` 因为 xd 复用必须**重命名**为 `Config.XD_BZH_*` 而不能删；`pz`/`qs` 是背驰**类型**保留不动。每个任务独立可 revert。

**Tech Stack:** Python 3.10 / poetry / pytest / Flask / TradingView UDF datafeed (TS+webpack)

**关联设计文档：** `docs/superpowers/specs/2026-05-06-remove-zsd-qsd-design.md`

---

## Pre-Task 0: 准备工作

**Files:**
- 无

- [ ] **Step 0.1: 确认工作分支干净**

```bash
git status
git branch --show-current
```

Expected: 当前在 `master` 分支或专门的 `chore/remove-zsd-qsd` 分支，无未提交改动（除已知的 `.omc/`、`docs/superpowers/plans/` 新文件）。

如果想隔离工作，建议用 worktree：

```bash
git worktree add ../chanlun-pro-remove-zsd master -b chore/remove-zsd-qsd
cd ../chanlun-pro-remove-zsd
```

- [ ] **Step 0.2: 跑一遍现有测试，记录基线**

```bash
D:/software/Python310/python.exe -m pytest -x --tb=short 2>&1 | tail -30
```

Expected: 测试通过或已知失败用例列表。把当前 PASS/FAIL/SKIP 数量记下来，作为后续 regression 对比基线。

- [ ] **Step 0.3: 备份 bundle.js**

```bash
cp web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js \
   web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js.bak
```

Expected: 备份文件创建成功。**Task 5 完成验收后才能删除 .bak。**

---

## Task 1: 核心层 — cl_interface / cl / bs_point_calculator

**目标：** 重命名 ZSD_BZH 枚举为 XD_BZH（xd 复用），删除 ZSD_QJ_* 枚举、抽象方法、ZS/BC 中的 zsd/qsd 取值。

**Files:**
- Modify: `src/chanlun/core/cl_interface.py:65-70, 522-525, 742-745, 1584-1622`
- Modify: `src/chanlun/core/cl.py:67-90, 112-115, 276-312, 527-554`
- Modify: `src/chanlun/core/bs_point_calculator.py:60-66`

### Step 1.1: cl_interface.py — 重命名 ZSD_BZH，删除 ZSD_QJ

- [ ] **修改 `src/chanlun/core/cl_interface.py` 第 65-70 行**

**Before:**
```python
    # 走势段配置项
    ZSD_BZH_NO = "zsd_bzh_no"  # TODO 移除配置。走势段不进行标准化
    ZSD_BZH_YES = "zsd_bzh_yes"  # TODO 移除配置。走势段进行标准化
    ZSD_QJ_DD = "zsd_qj_dd"  # 走势段区间，使用线段的顶底点作为区间
    ZSD_QJ_CK = "zsd_qj_ck"  # 走势段区间，使用线段中缠论K线的最高最低作为区间
    ZSD_QJ_K = "zsd_qj_k"  # 走势段区间，使用线段中原始K线的最高最低作为区间
```

**After:**
```python
    # 线段标准化配置项（xd 复用，原 ZSD_BZH_*）
    XD_BZH_NO = "xd_bzh_no"  # 线段不进行标准化
    XD_BZH_YES = "xd_bzh_yes"  # 线段进行标准化
```

### Step 1.2: cl_interface.py — 收窄 ZS.zs_type、BC.type 取值

- [ ] **修改 `cl_interface.py:522-525` 的 ZS 构造器注释**

把 `# 'bi' 笔中枢, 'xd' 线段中枢, 'zsd' 走势段中枢` 改为 `# 'bi' 笔中枢, 'xd' 线段中枢`。

- [ ] **修改 `cl_interface.py:742-745` 的 BC 类型注释**

把 `# 背驰类型 （bi 笔背驰 xd 线段背驰 zsd 走势段背驰 pz 盘整背驰 qs 趋势背驰）`
改为 `# 背驰类型 （bi 笔背驰 xd 线段背驰 pz 盘整背驰 qs 趋势背驰）`。

### Step 1.3: cl_interface.py — 删除抽象方法

- [ ] **删除 `cl_interface.py` 中的四个抽象方法**

删除：
- `get_zsds()` (≈ line 1584-1590)
- `get_zsd_zss()` (≈ line 1614-1618)
- `get_qsds()` (≈ line 1592-1598)
- `get_qsd_zss()` (≈ line 1620-1626)

完整删除 `@abstractmethod` 装饰器、方法签名、docstring、`pass`。保留前后空行规范。

### Step 1.4: cl.py — 删除 zsd/qsd 字段、方法、属性，更新 xd_bzh 默认值

- [ ] **修改 `src/chanlun/core/cl.py:67-90`**（构造器内字段）

**删除以下行：**
```python
        # self.zsds: List[XD] = []  # 走势段列表
        # self.qsds: List[XD] = []  # 趋势段列表
```
```python
        self.zsd_zss: List[ZS] = []  # 走势段中枢
        self.qsd_zss: List[ZS] = []  # 趋势段中枢
```

- [ ] **修改 `cl.py:114`（默认配置）**

**Before:**
```python
            'xd_bzh': Config.ZSD_BZH_YES.value,
```

**After:**
```python
            'xd_bzh': Config.XD_BZH_YES.value,
```

- [ ] **修改 `cl.py:276-282, 305-311`（删除 get_zsds / get_qsds / get_zsd_zss / get_qsd_zss 实现）**

完整删除以下四个方法（含 docstring 与空行）：
```python
    def get_zsds(self) -> List[XD]:
        """返回走势段列表"""
        return []

    def get_qsds(self) -> List[XD]:
        """返回趋势段列表"""
        return []
```
和
```python
    def get_zsd_zss(self) -> List[ZS]:
        """返回走势段中枢列表"""
        return self.zsd_zss

    def get_qsd_zss(self) -> List[ZS]:
        """返回趋势段中枢列表"""
        return self.qsd_zss
```

- [ ] **修改 `cl.py:527-554`（删除 zsds / qsds 属性、type_zsd_zss / type_qsd_zss）**

完整删除：
```python
    @property
    def zsds(self) -> List[XD]:
        return self.get_zsds()
```
```python
    @property
    def qsds(self) -> List[XD]:
        return self.get_qsds()
```
```python
    @property
    def type_zsd_zss(self) -> dict:
        return {Config.ZS_TYPE_BZ.value: self.get_zsd_zss()}
```
和 `type_qsd_zss` 同等价物（如有）。用 grep 二次确认。

### Step 1.5: bs_point_calculator.py — 收窄 zs_type 校验

- [ ] **修改 `src/chanlun/core/bs_point_calculator.py:60-66`**

**Before:**
```python
        if zs_type not in ('bi', 'xd', 'zsd'):
            raise ValueError(
                f"zs_type 必须是 'bi' / 'xd' / 'zsd' 之一, 当前传入: {zs_type}"
            )
```

**After:**
```python
        if zs_type not in ('bi', 'xd'):
            raise ValueError(
                f"zs_type 必须是 'bi' / 'xd' 之一, 当前传入: {zs_type}"
            )
```

### Step 1.6: 验证核心层

- [ ] **静态扫描确认无残留**

```bash
grep -rn -E "ZSD_BZH|ZSD_QJ|get_zsds|get_qsds|get_zsd_zss|get_qsd_zss|zsd_zss|qsd_zss" src/chanlun/core/
```

Expected: 仅命中重命名后的 `XD_BZH_*`（如有显式 `XD_BZH` 字符串）。其他全部消失。

- [ ] **导入冒烟测试**

```bash
D:/software/Python310/python.exe -c "from chanlun.core.cl import CL; from chanlun.core.cl_interface import Config; assert Config.XD_BZH_YES.value == 'xd_bzh_yes'; print('OK')"
```

Expected: `OK`，无 `AttributeError` / `ImportError`。

- [ ] **跑核心相关测试**

```bash
D:/software/Python310/python.exe -m pytest tests/ -k "cl or bi or xd or zs" -x --tb=short 2>&1 | tail -40
```

Expected: 通过率不低于 Pre-Task 0.2 的基线。

### Step 1.7: 提交

- [ ] **commit**

```bash
git add src/chanlun/core/cl_interface.py src/chanlun/core/cl.py src/chanlun/core/bs_point_calculator.py
git commit -m "$(cat <<'EOF'
refactor(core): 删除 zsd/qsd 级别，重命名 ZSD_BZH → XD_BZH

- cl_interface: 删除 ZSD_QJ_* / get_zsds / get_zsd_zss / get_qsds /
  get_qsd_zss 抽象方法，收窄 ZS.zs_type / BC.type 取值集合
- cl_interface: ZSD_BZH_* 重命名为 XD_BZH_*（xd 标准化复用此枚举，
  字符串值由 zsd_bzh_* 改为 xd_bzh_*）
- cl: 删除 zsd_zss / qsd_zss 字段、get_zsds / get_qsds /
  get_zsd_zss / get_qsd_zss 方法、zsds / qsds / type_zsd_zss /
  type_qsd_zss 属性，'xd_bzh' 默认值同步切换到 XD_BZH_YES
- bs_point_calculator: zs_type 校验从 (bi,xd,zsd) 收窄为 (bi,xd)
EOF
)"
```

---

## Task 2: 工具层 — cl_utils / file_db / kcharts

**Files:**
- Modify: `src/chanlun/cl_utils.py:357-360, 403-418, 800-820, 859-882, 964-970, 1016-1020`（grep 全文核对）
- Modify: `src/chanlun/file_db.py:113-115, 192-194`
- Modify: `src/chanlun/kcharts.py`（grep 定位）

### Step 2.1: cl_utils.py — 删除默认配置中的 zsd/qsd 键

- [ ] **修改 `cl_utils.py:357-360, 403-418` 区域**

**删除以下默认配置键：**
- `"zsd_qj": Config.ZSD_QJ_DD.value` （≈ line 359）
- `"chart_show_zsd": "1"` 与 `"chart_show_qsd": "0"` （≈ line 405-406）
- `"chart_show_zsd_zs": "0"` 与 `"chart_show_qsd_zs": "0"` （≈ line 409-410）
- `"chart_show_zsd_mmd": "1"` 与 `"chart_show_qsd_mmd": "1"` （≈ line 413-414）
- `"chart_show_zsd_bc": "1"` 与 `"chart_show_qsd_bc": "1"` （≈ line 417-418）

### Step 2.2: cl_utils.py — 删除图表数据生成

- [ ] **删除 `zsd_chart_data` 收集块（≈ line 800-820）**

完整删除：
```python
    zsd_chart_data = []
    if config["chart_show_zsd"] == "1":
        for zsd in cd.get_zsds():
            zsd_chart_data.append(
                {
                    "points": [
                        {"time": fun.datetime_to_int(zsd.start.k.date), "price": zsd.start.val},
                        {"time": fun.datetime_to_int(zsd.end.k.date), "price": zsd.end.val},
                    ],
                    "linestyle": "0" if zsd.is_done() else "1",
                }
            )
```

- [ ] **删除 qsd 同名等价物（如有）**

用 grep 二次确认：
```bash
grep -n "qsd_chart_data\|chart_show_qsd\b" src/chanlun/cl_utils.py
```
有命中则按 zsd 同样模式删除。

- [ ] **删除 `zsd_zs_chart_data` 收集块（≈ line 859-877）**

完整删除以 `zsd_zs_chart_data = []` 开头、以闭合花括号结束的整个块。同步删除 `qsd_zs_chart_data`（如有）。

- [ ] **修改 `cl_utils.py:878-888` 多级别 line/bc 类型 map**

```python
        "bi": cd.get_bis(),
        "xd": cd.get_xds(),
        "zsd": cd.get_zsds(),  # 删除此行
    }
    line_type_map = {"bi": "笔", "xd": "段", "zsd": "走", "qsd": "趋"}  # 改为 {"bi": "笔", "xd": "段"}
    bc_type_map = {
        "bi": "BI",
        "xd": "XD",
        "zsd": "ZSD",  # 删除此行
        "qsd": "QSD",  # 删除此行
        "pz": "PZ",     # 保留
        "qs": "QS",     # 保留
        ...
```

- [ ] **修改 `cl_utils.py:964-970` 排序**

删除：
```python
    zsd_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    zsd_zs_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
```
qsd 同上（如有）。

- [ ] **修改 `cl_utils.py:1016-1020` 输出字典**

删除返回 dict 中的：
```python
        "zsds": zsd_chart_data,
        "zsd_zss": zsd_zs_chart_data,
```
qsd 同上（如有）。

### Step 2.3: file_db.py — 删除缓存合并键

- [ ] **修改 `src/chanlun/file_db.py:113-115`**

**Before:**
```python
            "xd_qj",
            "zsd_qj",
            "xd_allow_bi_pohuai",
```

**After:**
```python
            "xd_qj",
            "xd_allow_bi_pohuai",
```

- [ ] **修改 `file_db.py:192-194`**

**Before:**
```python
    for key in ["fxs", "bis", "xds", "zsds", "bi_zss", "xd_zss", "zsd_zss", "bcs", "mmds"]:
```

**After:**
```python
    for key in ["fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds"]:
```

如有 `qsds` / `qsd_zss` 命中，一并去除。

### Step 2.4: kcharts.py — 删除 zsd/qsd 渲染分支

- [ ] **grep 定位 + 删除**

```bash
grep -n -E "zsd|qsd|走势段|趋势段" src/chanlun/kcharts.py
```

对每个命中：如果是图表渲染条件分支（如 `if config["chart_show_zsd"]==...`）整段删除；如果只是注释/标签，按上下文清理。无歧义条件下按 zsd/qsd 一一对应删除。

### Step 2.5: 验证工具层

- [ ] **静态扫描**

```bash
grep -rn -E "zsd|qsd|走势段|趋势段|chart_show_zsd|chart_show_qsd" src/chanlun/cl_utils.py src/chanlun/file_db.py src/chanlun/kcharts.py
```

Expected: 零命中。

- [ ] **导入冒烟测试 + 跑测试**

```bash
D:/software/Python310/python.exe -c "import chanlun.cl_utils, chanlun.file_db, chanlun.kcharts; print('OK')"
D:/software/Python310/python.exe -m pytest -x --tb=short 2>&1 | tail -30
```

Expected: 导入 OK，测试不低于基线。

### Step 2.6: 提交

- [ ] **commit**

```bash
git add src/chanlun/cl_utils.py src/chanlun/file_db.py src/chanlun/kcharts.py
git commit -m "refactor(utils): 删除 zsd/qsd 默认配置、图表数据、缓存合并键"
```

---

## Task 3: 策略层 — 删除 zsd 策略 + 降级 a_xd 策略

**Files:**
- Delete: `src/chanlun/strategy/strategy_zsd_xd_bi_1mmd.py`
- Modify: `src/chanlun/strategy/strategy_a_xd_trade_model.py:223-280`

### Step 3.1: 删除 strategy_zsd_xd_bi_1mmd.py

- [ ] **删除整个策略文件**

```bash
git rm src/chanlun/strategy/strategy_zsd_xd_bi_1mmd.py
```

- [ ] **核对无外部 import**

```bash
grep -rn "strategy_zsd_xd_bi_1mmd\|StrategyZsdXdBi1Mmd" src/ web/ script/ tests/
```

Expected: 零命中。如有命中，删除 import 行（属于死引用）。

### Step 3.2: 降级 strategy_a_xd_trade_model.py

- [ ] **修改 `src/chanlun/strategy/strategy_a_xd_trade_model.py:223-280`**

**核心改写规则（决策 4.1=b）：**

| 原代码 | 改为 |
|---|---|
| `cd_30m.get_zsds()` | `cd_30m.get_xds()` |
| `cd_5m.get_zsds()` | `cd_5m.get_xds()` |
| 局部变量 `zsds_down_30m` / `zsds_down_5m` | `xds_down_30m_confirm` / `xds_down_5m_confirm` |
| 局部变量 `_zsd` | `_xd_confirm` |
| `_zsd.bc_exists(["zsd", "pz", "qs"])` | `_xd_confirm.bc_exists(["xd", "pz", "qs"])` |
| `info["day_zsd_type"]` | `info["day_xd_type"]` |
| 注释里"走势段" | 改为"线段" |

**Before（≈ line 230-244）：**
```python
        zsds_down_30m = [
            _zsd
            for _zsd in cd_30m.get_zsds()
            if _zsd.start.k.date >= xd_day.start.k.date and _zsd.type == "down"
        ]
```

**After:**
```python
        xds_down_30m_confirm = [
            _xd_confirm
            for _xd_confirm in cd_30m.get_xds()
            if _xd_confirm.start.k.date >= xd_day.start.k.date and _xd_confirm.type == "down"
        ]
```

5m 段落同样改写。

**Before（≈ line 253-256）：**
```python
        for _zsd in zsds_down_30m[-2:]:
            if _zsd.mmd_exists(["1buy", "2buy", "3buy"]) or _zsd.bc_exists(
                ["zsd", "pz", "qs"]
            ):
```

**After:**
```python
        for _xd_confirm in xds_down_30m_confirm[-2:]:
            if _xd_confirm.mmd_exists(["1buy", "2buy", "3buy"]) or _xd_confirm.bc_exists(
                ["xd", "pz", "qs"]
            ):
```

**Before（≈ line 274-276）：**
```python
            "day_zsd_type": (
                0 if len(cd_day.get_zsds()) == 0 else cd_day.get_zsds()[-1].type
            ),
```

**After:**
```python
            "day_xd_type": (
                0 if len(cd_day.get_xds()) == 0 else cd_day.get_xds()[-1].type
            ),
```

如果 `info` 字典的 key 在策略外部被消费（如日志、回测报表），需要同步更新消费方。grep 确认：

```bash
grep -rn "day_zsd_type" src/ web/ script/ tests/ notebook/
```

如仅在本文件内使用，安全改名。如有外部消费方，把消费方一并改为 `day_xd_type`。

### Step 3.3: 验证策略层

- [ ] **静态扫描**

```bash
grep -rn -E "zsd|qsd|走势段|趋势段" src/chanlun/strategy/
```

Expected: 零命中。

- [ ] **策略 import 冒烟**

```bash
D:/software/Python310/python.exe -c "from chanlun.strategy.strategy_a_xd_trade_model import *; print('OK')"
```

Expected: 无 `AttributeError`、无 `ImportError`。

- [ ] **如有策略相关测试，跑一次**

```bash
D:/software/Python310/python.exe -m pytest tests/ -k "strategy" -x --tb=short 2>&1 | tail -20
```

Expected: 通过，或失败用例数与基线一致（不引入新失败）。

### Step 3.4: 提交

- [ ] **commit**

```bash
git add src/chanlun/strategy/
git commit -m "$(cat <<'EOF'
refactor(strategy): 删除 zsd 策略，a_xd 策略降级为线段确认

- 删除 strategy_zsd_xd_bi_1mmd.py（整体围绕 zsd 构建，已无意义）
- strategy_a_xd_trade_model: 30m/5m 多级别确认从 get_zsds() 降级为
  get_xds()，bc_exists 类型由 ['zsd','pz','qs'] 改为 ['xd','pz','qs']，
  保留多级别交叉确认意图，info["day_zsd_type"] → info["day_xd_type"]
EOF
)"
```

---

## Task 4: Web/UI/脚本

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`
- Modify: `web/chanlun_chart/cl_app/blueprints/tv.py`
- Modify: `web/chanlun_chart/cl_app/blueprints/options.py`
- Modify: `web/chanlun_chart/cl_app/templates/options.html`
- Modify: `web/chanlun_chart/cl_app/static/js/charts.js`
- Modify: `web/chanlun_chart/cl_app/static/datafeeds/udf/src/history-provider.ts`
- Modify: `script/trader/reboot_trader_currency.py`
- Modify: `script/trader/reboot_trader_a_stock.py`
- Modify: `script/trader/reboot_trader_hk_stock.py`
- Modify: `script/trader/reboot_trader_futures.py`
- Modify: `script/trader/reboot_trader_ctp.py`

### Step 4.1: chart_compute.py — 删除 zsd/qsd 字段读写

- [ ] **grep 全文定位**

```bash
grep -n -E "zsd|qsd|走势段|趋势段" web/chanlun_chart/cl_app/services/chart_compute.py
```

- [ ] **逐处删除**

对每个命中：
- 如果是请求参数读取（`request.args.get("chart_show_zsd")` 等）→ 删除
- 如果是返回字段（`"zsds": ...`）→ 删除该 key
- 如果是配置传递（`zsd_qj`）→ 删除

### Step 4.2: tv.py / options.py — 删除 zsd/qsd 读取

- [ ] **同样 grep + 逐处删除**

```bash
grep -n -E "zsd|qsd|走势段|趋势段|chart_show_zsd|chart_show_qsd" \
  web/chanlun_chart/cl_app/blueprints/tv.py \
  web/chanlun_chart/cl_app/blueprints/options.py
```

按命中类型对应删除（参数读取、传递、写入 cookies / config）。

### Step 4.3: options.html — 删除 UI 选项

- [ ] **删除"走势段区间"下拉块（≈ line 246-258）**

完整删除 `<div class="layui-inline" title="走势段区间..."` 整个 div 块（直到对应 `</div>` 闭合，包含 `<select name="zsd_qj">` 与三个 option）。

- [ ] **修改"线段区间"行的 title（≈ line 232）**

**Before:**
```html
            <div class="layui-inline" title="线段区间计算依据，影响走势段的特征序列计算">
```

**After:**
```html
            <div class="layui-inline" title="线段区间计算依据，影响线段的特征序列计算">
```

- [ ] **删除走势段/趋势段 checkbox（≈ line 565-632）**

逐对删除以下 8 个 checkbox 所在的 `<div class="layui-col-xs3">` 块：

- `name="chart_show_zsd"` / `name="chart_show_qsd"`
- `name="chart_show_zsd_zs"` / `name="chart_show_qsd_zs"`
- `name="chart_show_zsd_mmd"` / `name="chart_show_qsd_mmd"`
- `name="chart_show_zsd_bc"` / `name="chart_show_qsd_bc"`

注意：layui 一行 4 列，删除 8 个 col-xs3 后可能需要调整周围 row 结构以避免视觉错位。删除后用浏览器看一眼布局是否正常（在 Step 4.7 验证时一并看）。

- [ ] **修改配置预设保存列表（≈ line 863）**

删除列表中的 `"zsd_qj",` 项。

### Step 4.4: charts.js — 删除 zsd/qsd 渲染

- [ ] **grep + 删除**

```bash
grep -n -E "zsd|qsd|走势段|趋势段|chart_show_zsd|chart_show_qsd" web/chanlun_chart/cl_app/static/js/charts.js
```

每个命中按"渲染分支整段删除"原则处理。注意 JS 没有强类型，确保删除后的对象访问不会因 `obj.zsds` 不存在而报错（应直接不再访问该字段）。

### Step 4.5: history-provider.ts — 删除字段读取

- [ ] **grep + 删除**

```bash
grep -n -E "zsd|qsd" web/chanlun_chart/cl_app/static/datafeeds/udf/src/history-provider.ts
```

按 TypeScript 类型定义同步删除字段（接口/类型定义中的 `zsds?: ...` 等）+ 使用点。

### Step 4.6: 5 个 trader 脚本 — 删除 2 行配置

对 **每一个** 文件：

- [ ] `script/trader/reboot_trader_currency.py` 第 34-35 行
- [ ] `script/trader/reboot_trader_a_stock.py` 第 629-630 行
- [ ] `script/trader/reboot_trader_hk_stock.py` 第 96-97 行
- [ ] `script/trader/reboot_trader_futures.py` 第 34-35 行
- [ ] `script/trader/reboot_trader_ctp.py` 第 31-32 行

**删除以下 2 行：**
```python
        "zsd_bzh": Config.ZSD_BZH_NO.value,
        "zsd_qj": Config.ZSD_QJ_DD.value,
```

### Step 4.7: 验证 Web/脚本层

- [ ] **静态扫描**

```bash
grep -rn -E "zsd|qsd|走势段|趋势段|chart_show_zsd|chart_show_qsd" \
  web/chanlun_chart/cl_app/ \
  script/trader/ \
  --include='*.py' --include='*.html' --include='*.js' --include='*.ts'
```

Expected: 零命中（bundle.js 留到 Task 5 处理）。

- [ ] **trader 脚本 import 冒烟**

```bash
for f in script/trader/reboot_trader_currency.py \
         script/trader/reboot_trader_a_stock.py \
         script/trader/reboot_trader_hk_stock.py \
         script/trader/reboot_trader_futures.py \
         script/trader/reboot_trader_ctp.py; do
  D:/software/Python310/python.exe -c "import ast; ast.parse(open('$f', encoding='utf-8').read()); print('$f OK')"
done
```

Expected: 5 个 OK。

- [ ] **启动 web 服务并人工验证**

```bash
# 启动命令以项目实际为准（如 web/chanlun_chart/main.py 或 flask run）
D:/software/Python310/python.exe web/chanlun_chart/main.py &
```

浏览器访问：
- options 页面：确认无"走势段"/"趋势段"字眼，保存配置不报错
- 主图表页：图表加载，浏览器 console 无报错（特别是 `chart_show_zsd is undefined` 类）

注意：此时 bundle.js 还没改，可能仍有旧字符串但应不被触发。Task 5 完成才是最终前端验收。

### Step 4.8: 提交

- [ ] **commit**

```bash
git add web/chanlun_chart/cl_app/services/ web/chanlun_chart/cl_app/blueprints/ \
        web/chanlun_chart/cl_app/templates/ \
        web/chanlun_chart/cl_app/static/js/ \
        web/chanlun_chart/cl_app/static/datafeeds/udf/src/ \
        script/trader/
git commit -m "refactor(web,trader): 删除 zsd/qsd UI 选项、字段、脚本配置"
```

---

## Task 5: bundle.js 手工 patch（高风险）

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js`

### Step 5.1: 备份确认

- [ ] **确认 .bak 已存在（Pre-Task 0.3）**

```bash
ls -la web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js.bak
```

Expected: 文件存在。如不存在，立即重新备份后再开始 patch。

### Step 5.2: grep 定位字符串字面量

- [ ] **找出可安全 patch 的位置**

```bash
grep -no -E "[\"']zsd[s_]?[\"']|[\"']qsd[s_]?[\"']|[\"']zsd_zss[\"']|[\"']qsd_zss[\"']|[\"']chart_show_zsd[a-z_]*[\"']|[\"']chart_show_qsd[a-z_]*[\"']" \
  web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js
```

记录每个命中的行号与字符串。仅对**作为对象 key 使用的字符串字面量**进行 patch（这类未被 mangling 的概率最高）。

### Step 5.3: 逐个 patch

对每个命中：

- [ ] **判断上下文**：是 `{"zsds": data.zsds}` 这种 key？还是被代码逻辑直接消费的字符串？
- [ ] **仅 patch 安全位置**：
  - 对象字面量 key → 整 key+value 一并删除（注意逗号）
  - 数组中的字符串元素 → 删除该元素 + 前后逗号
  - 如果是变量赋值（`var x = "zsds"`）→ 跳过，可能被压缩为外部引用，不可单独 patch

### Step 5.4: 浏览器验证

- [ ] **重启 web 服务，浏览器硬刷新**

打开主图表页，浏览器 DevTools：
- Console：无 `Uncaught ReferenceError` / `Uncaught TypeError`
- Network：history-provider 请求返回 200，无字段缺失警告
- 图表：可正常加载、缩放、切换 symbol

- [ ] **如出现报错 → 立即回滚**

```bash
cp web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js.bak \
   web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js
```

回退到 4.2=a 方案：bundle.js 不动，在 UPDATE.md 注明"bundle.js 需重新构建"。

### Step 5.5: 验收成功后清理 .bak

- [ ] **删除备份**

```bash
rm web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js.bak
```

### Step 5.6: 提交

- [ ] **commit**

成功 patch：
```bash
git add web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js
git commit -m "chore(web): 手工 patch bundle.js 删除 zsd/qsd 字段引用"
```

回退到 4.2=a：
```bash
# bundle.js 未改，无需提交。在 Task 6 的 UPDATE.md 加注释。
```

---

## Task 6: 文档与 notebook

**Files:**
- Modify: `cookbook/docs/UPDATE.md`
- Modify: `cookbook/docs/缠论买卖点和背驰规则.md`
- Modify: `cookbook/docs/缠论配置项说明.md`
- Modify: `cookbook/docs/缠论数据对象与方法.md`
- Modify: `cookbook/docs/index.md`
- Modify: `cookbook/docs/计算性能.md`
- Modify: `cookbook/docs/多中枢类型相同买卖点策略.md`（审阅决定整删/部分删）
- Modify: `cookbook/docs/合成自定义K线数据（分钟）.md`
- Modify: `cookbook/docs/基于线段的中枢震荡策略.md`
- Modify: `notebook/回测_沪深股票策略.ipynb`
- Modify: `notebook/回测_缠论参数优化.ipynb`
- Modify: `notebook/回测_期货策略.ipynb`

### Step 6.1: 配置/方法说明文档

- [ ] **`缠论配置项说明.md`：删除 `zsd_bzh`、`zsd_qj`、`chart_show_zsd*`、`chart_show_qsd*` 条目**

grep 定位段落，删除对应表格行/列表项/小节。

- [ ] **`缠论数据对象与方法.md`：删除 `get_zsds`、`get_qsds`、`get_zsd_zss`、`get_qsd_zss` 方法说明**

如有"走势段中枢"相关属性 (`zsd_zss`)，一并删除。

- [ ] **`缠论买卖点和背驰规则.md`：删除走势段/趋势段相关章节**

注意：保留 pz（盘整背驰）、qs（趋势背驰）的说明。

### Step 6.2: 索引/性能/策略文档

- [ ] **`index.md`：删除目录中"走势段"/"趋势段"条目**

- [ ] **`计算性能.md`：删除 zsd/qsd 性能数据**（如有）

- [ ] **`多中枢类型相同买卖点策略.md`：审阅决定**

读完整篇判断：
- 主体围绕 zsd/qsd → 整篇删除（`git rm` 该文件 + 从 `index.md` 移除入口）
- 主体围绕 bi/xd 中枢，zsd/qsd 仅旁注 → 仅删旁注

- [ ] **`合成自定义K线数据（分钟）.md`：删 zsd/qsd 配置说明段**

- [ ] **`基于线段的中枢震荡策略.md`：标题表明主体是 xd 中枢，仅删 zsd/qsd 旁注**

### Step 6.3: UPDATE.md changelog

- [ ] **在 `cookbook/docs/UPDATE.md` 顶部新增条目**

格式参考现有变更日志风格，建议内容：

```markdown
### 2026-05-06 删除走势段 (zsd) 与趋势段 (qsd)

- 已彻底移除走势段、趋势段两级结构（核心代码、配置、UI、策略、文档）
- 现有用户配置文件中残留的 `chart_show_zsd_*`、`chart_show_qsd_*`、`zsd_qj`、`zsd_bzh` 等键将被静默忽略
- 旧缓存文件中的 `zsds`、`zsd_zss`、`qsds`、`qsd_zss` 字段将被丢弃，不影响新缓存生成
- 旧 notebook (`回测_*.ipynb`) 涉及 zsd/qsd 的 cell 已删除，旧执行输出可能与新代码不一致，需要重新跑
- 配置枚举 `Config.ZSD_BZH_*` 重命名为 `Config.XD_BZH_*`（xd 标准化复用此枚举），字符串值由 `zsd_bzh_*` 改为 `xd_bzh_*`
- 策略 `strategy_zsd_xd_bi_1mmd` 已删除；`strategy_a_xd_trade_model` 多级别确认由走势段降级为线段（`get_zsds()` → `get_xds()`，`bc_exists(["zsd","pz","qs"])` → `bc_exists(["xd","pz","qs"])`）
- 保留：盘整背驰 (`pz`)、趋势背驰 (`qs`) 仍是 bi/xd 上的有效背驰类型
```

如 Task 5 回退到 4.2=a，再加一条：

```markdown
- ⚠️ TradingView datafeed bundle (`web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js`) 需要重新构建以彻底清除 zsd/qsd 字段引用
```

### Step 6.4: notebook 修改

**注意：.ipynb 是 JSON，不能用纯字符串替换。** 用 nbformat 库或精确 JSON 编辑。

- [ ] **使用脚本批处理**

写一个临时脚本（用完即删，不提交）：

```python
# /tmp/strip_zsd_qsd.py
import json
import sys
from pathlib import Path

PATTERNS = [
    "zsd_bzh", "zsd_qj",
    "chart_show_zsd", "chart_show_qsd",
    "get_zsds", "get_qsds",
    "get_zsd_zss", "get_qsd_zss",
    "走势段", "趋势段",
]

def cell_should_drop(cell) -> bool:
    src = "".join(cell.get("source", []))
    return any(p in src for p in PATTERNS)

def cell_strip_lines(cell) -> bool:
    """对源里仅部分行命中的 cell，删除命中行；返回是否被修改。"""
    src = cell.get("source", [])
    new_src = [line for line in src if not any(p in line for p in PATTERNS)]
    if len(new_src) != len(src):
        cell["source"] = new_src
        return True
    return False

for path in sys.argv[1:]:
    nb = json.loads(Path(path).read_text(encoding="utf-8"))
    new_cells = []
    for cell in nb["cells"]:
        if cell.get("cell_type") in ("code", "markdown") and cell_should_drop(cell):
            # 整体围绕 zsd/qsd 的 cell：先尝试只删命中行；若 cell 被掏空则丢弃
            cell_strip_lines(cell)
            if "".join(cell.get("source", [])).strip():
                new_cells.append(cell)
            # else: drop entirely
        else:
            new_cells.append(cell)
    nb["cells"] = new_cells
    Path(path).write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"updated: {path}")
```

运行：

```bash
D:/software/Python310/python.exe /tmp/strip_zsd_qsd.py \
  notebook/回测_沪深股票策略.ipynb \
  notebook/回测_缠论参数优化.ipynb \
  notebook/回测_期货策略.ipynb
```

- [ ] **JSON 结构验证**

```bash
for f in notebook/回测_沪深股票策略.ipynb notebook/回测_缠论参数优化.ipynb notebook/回测_期货策略.ipynb; do
  D:/software/Python310/python.exe -c "import json; json.load(open('$f', encoding='utf-8')); print('$f valid JSON')"
done
```

Expected: 3 个 `valid JSON`。

- [ ] **可选：用 nbconvert 验证可加载**

```bash
D:/software/Python310/python.exe -m nbconvert --to notebook --stdout notebook/回测_沪深股票策略.ipynb > /dev/null && echo OK
```

- [ ] **删除临时脚本**

```bash
rm /tmp/strip_zsd_qsd.py
```

### Step 6.5: 验证文档/notebook

- [ ] **静态扫描**

```bash
grep -rn -E "zsd|qsd|走势段|趋势段" cookbook/docs/ notebook/
```

Expected: 仅命中 `cookbook/docs/UPDATE.md` 中本次新增的 changelog 条目（保留作为历史记录）。其他全部消失。

### Step 6.6: 提交

- [ ] **commit**

```bash
git add cookbook/docs/ notebook/
git commit -m "docs: 删除走势段/趋势段相关说明、notebook cell，更新 changelog"
```

---

## Task 7: 全局验收

### Step 7.1: 全仓最终扫描

- [ ] **生产代码与脚本**

```bash
grep -rn -E "zsd|qsd|走势段|趋势段|ZSD|QSD|chart_show_zsd|chart_show_qsd" \
  src/ web/ script/ \
  --include='*.py' --include='*.html' --include='*.js' --include='*.ts'
```

Expected: 仅以下场景有命中（每条都需手工核对）：
- bundle.js（如 Task 5 已 patch 应零命中；如回退到 4.2=a 则保留）
- 注释中纯历史变更说明（极少，按需保留）
- `pz` / `qs` 不应被 grep 命中（pattern 已排除）

如有意外命中 → 回到对应 Task 处理。

- [ ] **第三方包噪声排除确认**

`.codex_debug_venv/` 下命中（如 tqsdk 的 qsd 子串）属于第三方包，不在范围内。`poetry.lock` / `pyproject.toml` / `requirements.txt` 中的 `tqsdk` / `aqsdk` 等命中同样忽略。

### Step 7.2: 完整测试

- [ ] **跑全量 pytest**

```bash
D:/software/Python310/python.exe -m pytest --tb=short 2>&1 | tail -50
```

Expected: 通过率不低于 Pre-Task 0.2 基线。如新增失败用例，定位并修复。

- [ ] **跑 ruff lint**

```bash
ruff check src/ web/ script/ tests/
```

Expected: 无新增错误（已有错误数 ≤ 基线）。

### Step 7.3: 启动 web 完整冒烟

- [ ] **启动 web 服务**

```bash
D:/software/Python310/python.exe web/chanlun_chart/main.py
```

浏览器跑下列页面，每页都看 console 无报错：

- 主图表页（多个 symbol 切换、不同 frequency 切换）
- options 页（保存配置 / 重置配置）
- 策略列表页（如有）

- [ ] **trader 脚本 import 冒烟**（与 Step 4.7 重复但作为最终验收）

```bash
for f in script/trader/reboot_trader_*.py; do
  D:/software/Python310/python.exe -c "import ast; ast.parse(open('$f', encoding='utf-8').read())" && echo "$f OK"
done
```

Expected: 5 个 OK。

### Step 7.4: 验证旧用户配置兼容

- [ ] **构造一个含残留 zsd/qsd 键的 options.json**

```bash
cat > /tmp/legacy_options.json <<'EOF'
{
  "chart_show_zsd": "1",
  "chart_show_qsd": "1",
  "chart_show_zsd_zs": "0",
  "zsd_qj": "zsd_qj_dd",
  "xd_qj": "xd_qj_dd"
}
EOF
```

- [ ] **以这份 options 启动一次 chanlun 计算（用最小 demo）**

```bash
D:/software/Python310/python.exe -c "
import json
opts = json.load(open('/tmp/legacy_options.json', encoding='utf-8'))
from chanlun.core.cl import CL
cd = CL('TEST.SH', '1d', opts)
print('OK, residual keys silently ignored')
"
```

Expected: `OK, residual keys silently ignored`，无 `KeyError` / `AttributeError`。

- [ ] **清理**

```bash
rm /tmp/legacy_options.json
```

### Step 7.5: PR / 合入准备（可选）

- [ ] **commit 历史核对**

```bash
git log --oneline master..HEAD
```

Expected: 5-6 个 commit（Task 1-6 各一个，Task 5 视情况），每条 message 描述清晰。

- [ ] **diff 总规模检查**

```bash
git diff master --stat
```

Expected: 删除行远多于新增行（典型清理 PR 形态）。

- [ ] **如需 PR，创建之**（仅用户明确要求时执行）

---

## 自审 Checklist

**1. Spec 覆盖：**

| Spec 章节 | 对应 Task |
|---|---|
| §2.1 核心代码 | Task 1 |
| §2.2 工具/配置 | Task 2 |
| §2.3 策略 | Task 3 |
| §2.4 Web 前后端（除 bundle.js） | Task 4 |
| §2.4 bundle.js | Task 5 |
| §2.5 交易脚本 | Task 4 (4.6) |
| §2.6 cookbook 文档 | Task 6 (6.1-6.3) |
| §2.7 notebook | Task 6 (6.4) |
| §3.1 ZSD_BZH 重命名 | Task 1 (1.1, 1.4) |
| §3.4 用户旧配置兼容 | Task 7 (7.4) |
| §4 不删除内容（pz/qs/_calc_qs） | 各 Task 中显式提示保留 |
| §6 验收标准 | Task 7 |

**2. Placeholder 扫描：** 全文无 TBD/TODO/"implement later"。bundle.js patch 步骤虽有"按上下文判断"的人工成分，但每一步给了明确判断准则与回滚方案。

**3. 类型/命名一致性：** `_xd_confirm` / `xds_down_30m_confirm` / `day_xd_type` 在 Task 3 多处统一；`Config.XD_BZH_*` 在 Task 1.1 / 1.4 一致。

---

## Plan complete.

设计文档：`docs/superpowers/specs/2026-05-06-remove-zsd-qsd-design.md`
本计划：`docs/superpowers/plans/2026-05-06-remove-zsd-qsd.md`
