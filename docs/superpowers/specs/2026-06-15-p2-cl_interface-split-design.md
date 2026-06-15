# P2 第一刀:拆分 cl_interface.py → core/types/ 包(设计 spec)

- 日期:2026-06-15
- 分支:`fix/zhongshu-l0`
- 状态:**待用户评审**
- 上游:[[2026-06-15-chanlun-optimization-design]] 路线图 P2(可读性/目录)
- 安全网:P0 黄金主(`tests/chan_core/`)+ P1 已删 15 文件

---

## 1. 背景与范围

P2 = 拆 4 个巨文件(`cl_interface.py` 1951 / `db.py` 1443 / `cl_utils.py` 1439 / `kcharts.py` 1416)+ 目录重组。按 brainstorm 惯例**先做一个**:`cl_interface.py`(最大、最中心、缝最干净)。用户选 **Option B**:不只拆文件,**真正把类挪进新目录**(`core/types/` 包),调方改用新路径——而非 facade 永久隐藏。

**本 spec 只覆盖 cl_interface.py。** db/cl_utils/kcharts 与更大目录重组 = 各自后续 sub-project。

## 2. 现状

`cl_interface.py`(1951 行)19 个顶层类 + 5 个函数,缝干净(见 §4 布局)。**44 个文件 import 它**(42 在 src/chanlun:19 在 core、其余散布;web/scripts 各 0),风格混杂含 3 处 `import *`。

**关键风险——pickle 兼容**:领域对象带 `__slots__` + 自定义 `__setstate__`,被 pickle 进缓存。`chart_cache` 存纯 OHLC(`fetch.py:3`,无对象,安全),但 **Signal 类对象会被 pickle**——`chanlun_selector.py:19` 已有注释 `# module alias used by older cached Signal objects`,证明团队**既有范式**=类挪模块后在旧路径留 module alias。移动 `cl_interface` 的类会改其 `__module__`,旧 pickle 加载时按旧 `__module__` 找类 → 必须旧路径仍可解析。

## 3. 目标 / 非目标

- **目标**:`cl_interface.py` 拆成 `core/types/` 内聚子模块;44 调方迁到新路径;**行为/pickle 100% 保持**(黄金主零漂移)。
- **非目标**:db/cl_utils/kcharts(后续);改任何缠论逻辑/数值;动 web/scripts 业务逻辑(仅被动改 import 路径,本例其实 0 处)。

## 4. 目标布局 `src/chanlun/core/types/`(依赖分层)

| 模块 | 内容(原行号) | 依赖 |
|---|---|---|
| `config.py` | `Config`(32) `Level`(97) | 无 |
| `kline.py` | `_slot_setstate`(21) `Kline`(108) `CLKline`(155) `FX`(238) | config |
| `line.py` | `LINE`(396) `BI`(867) `XD`(1189) `ZSLX`(1398) `TZXL`(1065) `XLFX`(1140) | kline |
| `zhongshu.py` | `ZS`(551) | line |
| `signal.py` | `MMD`(796) `BC`(824) | (运行期)zhongshu/line |
| `info.py` | `LOW_LEVEL_QS`(1451) `MACD_INFOS`(1486) `LINE_FORM_INFOS`(1508) `BW_LINE_QS_INFOS`(1548) | line/zhongshu |
| `interface.py` | `ICL`(1575) + `_resolve_ld_macd`(1757) `query_macd_ld`(1773) `compare_ld_beichi`(1825) `user_custom_mmd`(1864) | 全部 |
| `__init__.py` | 从各子模块再导出全部公开名(`__all__`) | — |

`line` 体系(LINE/BI/XD/ZSLX/TZXL/XLFX)紧耦合,整组不拆。

## 5. 循环 import 处理

`line ↔ zhongshu ↔ signal` 有互引(`BI.add_mmd`建 `MMD`、`MMD.__init__(name, zs)`引 `ZS`、`ZS.lines: List[LINE]`)。打成无环三招:
1. **加载序**:config→kline→line→zhongshu→signal→interface(模块顶层只 import 更底层)。
2. **运行期互引**(如 `BI.add_mmd` 造 `MMD`、`ZS.zs_mmds` 调 `line.line_mmds`)用**方法内局部 import**。
3. **类型注解**:每个新模块首行 `from __future__ import annotations`(注解字符串化)+ 需要时 `if TYPE_CHECKING:` 引入纯类型名。

## 6. facade(= pickle module-alias)

`cl_interface.py` 退化为:
```python
from chanlun.core.types import *          # noqa: F401,F403
from chanlun.core.types import __all__    # 显式转发
```
保留旧 import 路径 + **老 pickle 可解析**(旧 `__module__='chanlun.core.cl_interface'` 的对象加载时 `from chanlun.core.cl_interface import ZS` 命中 facade)。**facade 永久保留**(pyproject 已为它/`__init__` 配 F401/F403 豁免范式,新增 types/__init__ 与 cl_interface facade 同样加豁免)。

## 7. 两阶段迁移(各自门控)

- **阶段1 建包+搬类+facade**:创建 `core/types/` 7 模块,逐类搬运、修内部引用、按 §5 解环;`cl_interface.py` 改 facade。**此阶段行为/import/pickle 全不变**(调方仍走旧路径经 facade)。
- **阶段2 迁调方**:44 处 `from chanlun.core.cl_interface import X` → `from chanlun.core.types import X`(或更精确子模块)。按区域分批(core 内 19 → recursive_bt → 其余),每批一门控。facade 留着兜 pickle。

## 8. 门控(每阶段/每批)

1. **黄金主零漂移**:`pytest tests/`(P0 安全网,钉死 笔/段/中枢/买卖点/背驰)。
2. **import 烟雾**:`chanlun.core.cl`、`recursive_bt.engine/live_monitor`、`exchange/strategy/trader` 等活包全 import 成功。
3. **pickle 往返测试**(新增 `tests/chan_core/test_pickle_compat.py`):构造一个 CL→取 ZS/BI→`pickle.dumps` 后 `loads` 验证字段;并验证**旧路径** `chanlun.core.cl_interface.ZS is chanlun.core.types.zhongshu.ZS`(facade 同一类对象)。
4. **全量 `pytest tests/`** 绿。
5. **用户侧**:重启常驻 `live_monitor a/us` 验证实盘无碍(代码侧 import 不变,阶段2 后路径变需确认)。

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 循环 import | §5 三招;阶段1 完成即 import 烟雾验证 |
| 老 pickle 加载失败 | facade 永久保留(=既有 module-alias 范式);pickle 往返测试钉死 |
| 数值/行为漂移 | 纯搬运不改逻辑;黄金主零漂移门控 |
| live_monitor 中断 | 阶段1 import 零变化;阶段2 迁移后用户重启验证 |
| `import *` 调方(3处)拿不到名 | facade 与 `types/__init__` 都设 `__all__` 全量导出 |

## 10. 交付定义(P2 第一刀 Done)

- [ ] `core/types/` 7 模块建成,`cl_interface.py` 为 facade
- [ ] 44 调方迁到 `core.types`(facade 仅留作 pickle 兼容)
- [ ] `test_pickle_compat.py` 通过(新旧路径同类 + 往返)
- [ ] 黄金主零漂移 + import 烟雾 + 全量 pytest 绿
- [ ] 用户重启 live_monitor 验证
- [ ] 项目记忆更新(P2 第一刀完成 + 后续 db/cl_utils/kcharts 待)
