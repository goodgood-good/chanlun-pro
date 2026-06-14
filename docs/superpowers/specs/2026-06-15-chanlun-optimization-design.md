# src/chanlun 整体优化 —— 设计 spec(路线图 + Phase 0 安全网)

- 日期:2026-06-15
- 分支:`fix/zhongshu-l0`
- 状态:**待用户评审**
- 范围:`src/chanlun/`(150 个 .py / ~5.8 万行 / 8 子系统)

---

## 1. 背景与问题(为什么不能直接"优化")

用户诉求是把 `src/chanlun` **整体优化**,涵盖五个维度:性能、死代码清理、正确性/健壮性、可读性/可维护性、目录结构划分。

只读侦察暴露出一个**比"优化"严重得多的地基问题**:

| 事实 | 证据 | 含义 |
|---|---|---|
| 生产核心**零可用测试覆盖** | `pytest tests/` → 16 文件全部 collection-error(0.17s 中断) | 当前无任何回归保护 |
| 唯一测试套是**孤儿** | 16/16 测试 `import chan`(不存在的包),0 个 `import chanlun`;`src/chan` 在 git 全历史从未存在;`tests/fixtures/` 为空 | 它既不测生产代码,自己还把测试集体搞崩 |
| 生产核心 = `chanlun.core` | `recursive_bt` 8+ 文件 `from chanlun.core.cl import CL`,实盘/回测靠它 | 真正要保护的是 `chanlun/core`,不是 `chan` |
| `ruff` 全绿 | `All checks passed!` | lint 级清理已无空间,死代码须从**语义层**找(15 文件含 legacy 标记) |
| "结构乱"≈单文件臃肿 | `cl_interface.py` 1952 行、`db.py`/`cl_utils.py`/`kcharts.py` 各 1400+;目录本身扁平 | 重构重点是拆文件,不是加层级 |

**结论**:在一个真金白银跑实盘的系统上,没有回归安全网就做"目录重构 / 可读性重写 / 正确性修复",任何静默的行为漂移都可能直接进实盘。因此**第一步必须是建立针对生产核心的安全网**,而非任何"优化"。

> 注:此结论纠正了项目记忆中一条过时记录("910 passed")——该状态在当前工作树已不存在(疑似某次"从零重启"删了 `src/chan` 与 fixtures、却留下测试文件)。spec 通过后更新项目记忆。

---

## 2. 目标

1. **(P0)** 为生产核心 `chanlun.core.CL` 建立确定性的"特征化/黄金主"回归安全网,让 `pytest tests/` 变绿且有意义。
2. **(P1–P4)** 在安全网保护下,分阶段、风险递增地完成五项优化,**P0–P3 严格行为保持**,P4(唯一会改行为)单独 spec、最后做。

**非目标(本 spec 之外)**:重写一个干净的 `chan` 包(Option C);改变任何交易策略/信号语义;触碰 `web/`、`scripts/` 业务逻辑(仅在 P2 迁移 import 时被动跟随)。

---

## 3. 总体路线图(决策已定:先补真测试再优化)

风险递增、每步以"黄金主测试零漂移"为门控:

| 阶段 | 内容 | 风险 | 行为 | 产出 |
|---|---|---|---|---|
| **P0 安全网** | 特征化测试 `chanlun.core.CL` + 真实 parquet fixture + 隔离孤儿测试 | 低 | 不变(纯加测试) | 本 spec 详述 |
| **P1 死代码清理** | 15 个 legacy 文件、废弃路径(中枢重划残留、legacy MMD/zslx)、冗余文件、无用模块 | 低 | 不变 | 各自 spec |
| **P2 可读性/目录** | 拆 `cl_interface.py`(1952)等巨文件;用 **re-export shim 保旧路径**再渐进迁移 | **高**(牵连 web/scripts/常驻 live_monitor 的 import) | 不变 | 各自 spec |
| **P3 性能** | 延续热点路径 perf(局部、可独立验证) | 中 | 不变 | 各自 spec |
| **P4 正确性/健壮性** | bug、错误处理、边界 | **最高** | **可能改变** | 各自 spec(最后) |

每个 P1–P4 阶段在动手前各自走"brainstorm→spec→plan",本 spec 只把 **P0 钉死**。

---

## 4. Phase 0 详细设计

### 4.1 架构:特征化(Characterization)/ 黄金主(Golden-Master)测试

经典"给遗留代码加安全网"手法(Michael Feathers):不假设当前行为是否"正确",只**忠实快照当前生产行为**,锁住它。日后任何重构若改变了 笔/段/中枢/买卖点/背驰 的结构化输出,测试立即报警。

```
真实K线(固定窗口)
   └─[一次性]→ tests/fixtures/<sym>_<freq>.parquet  (提交进仓,确定性输入)
                     │
   测试运行时 ───────┤  df = pd.read_parquet(fixture)
                     ▼
        cl = CL(code, freq, config).process_klines(df)
                     │
                     ▼
        snapshot = {bis, xds, bi_zss, xd_zss, klines} 各元素 .to_dict()
                     │  canonicalize(排序键 + 定点 round 价格字段)
                     ▼
        与 tests/golden/<sym>_<freq>.json 逐字节比较
                     │
              漂移 → 测试失败(回归报警)
```

### 4.2 组件(单一职责、可独立理解)

1. **`tests/fixtures/`**(数据):每个 `(symbol, frequency)` 一个 parquet。内容是 `Exchange.klines()` 返回的原始 DataFrame,字段与 `process_klines` 期望一致(date/open/high/low/close/volume)。**parquet 而非 csv**(项目铁律:笔对 ~4e-16 浮点噪声敏感,csv 往返丢精度会改结构)。

2. **`tests/golden/`**(期望快照):每个 `(symbol, frequency)` 一个规范化 JSON,记录该输入下 `CL` 的全结构输出。**由 fixture + 当前生产代码一次性生成**,人工抽查后提交。

3. **`tests/tools/gen_fixtures.py`**(一次性脚本,非测试):从 exchange 适配器拉固定窗口 → 存 parquet;再跑 `CL` → 生成 golden JSON。带 `--update-golden` 开关。可复跑、幂等。

4. **`tests/chan_core/test_golden_master.py`**(回归测试):参数化遍历所有 fixture,重算并与 golden 比对。这是 P1–P4 的"回归报警器"。

5. **孤儿隔离**:`tests/chan/` → `tests/_spec_chan_clean/`,并在 `tests/conftest.py` 或 `pyproject` 配 `collect_ignore`/`--ignore`,使 `pytest` 不再 collection-error。**保留**作为未来"干净重写"的现成规格蓝图。

### 4.3 数据契约(已核实)

- 入口:`CL(code: str, frequency: str, config: dict).process_klines(klines: pd.DataFrame)`
- 取数:`Exchange.klines(code, frequency, ...) -> pd.DataFrame`
- 输出 getter(均返回带 `.to_dict()` 的对象):`get_klines()`、`get_bis() -> List[BI]`、`get_xds() -> List[XD]`、`get_bi_zss(zs_type) -> List[ZS]`、`get_xd_zss(zs_type) -> List[ZS]`
- **config 必须复刻生产**:从 `recursive_bt` 实际构造 `CL` 处取同一份 chan config,确保特征化的是"实盘行为"而非默认行为。

### 4.4 fixture 具体规格(默认值,评审时确认/微调)

| 维度 | 默认 | 理由 |
|---|---|---|
| A股标的 | 贵州茅台(项目内部 code,如 `SH.600519`)经 **QMT**(`ExchangeQMT`) | 高流动性、长历史;QMT 即实盘 A 股数据源,一次性拉取 |
| 美股标的 | `QQQ` 经 **cq / 长桥 Longbridge**(`ExchangeChangQiao`) | 实盘美股同源;ETF 干净;cq 支持 US/HK/A/FX 多市场 |
| 级别 | `d`(日线) + `30m`(30分钟) | 覆盖高/操作两级;同标的尽量同日历窗口 |
| 窗口 | 固定 `[start, end]` 日期区间,约 300–500 bars/(标的,级别) | 小而稳;区间写入 manifest 复现 |
| 总量 | 2 标的 × 2 级别 = 4 个 fixture(起步) | 够覆盖 笔/段/中枢/买卖点 各路径,又不臃肿 |

### 4.5 确定性处理(关键)

- **规范化 JSON**:`json.dumps(snapshot, sort_keys=True, ensure_ascii=False)`,消除键序差异。
- **浮点字段定点**:价格/数值类字段 `round(x, 8)`;结构身份字段(index/type/direction/计数)不 round。够灵敏检出真实结构变化,又压掉 1-ulp 平台噪声。
- **审计 `to_dict` 的非确定性源**:确认输出不经 `list(set(...))`/无序 dict(已初查:`to_dict` 走的是有序 list 字段,`line_mmds` 的 set 逻辑不入 `to_dict`——实现时再正式核一遍)。
- **golden 是平台相关的**:绑定当前 Windows + 当前 numpy/scipy 版本;若开发平台/依赖大版本变动,需 `--update-golden` 重生并人工复核。此约束写入 README。

### 4.6 验证安全网本身(元测试)

加网之后必须证明"网真的会响":
1. 全绿:`pytest tests/` 通过,4 个 golden-master 全过。
2. **变异检验**:临时对某价格字段 +1e-3 重算 → 测试必须**失败**(证明能抓到漂移);恢复。
3. 速度:整套 < 数秒(纯 CPU、读本地 parquet,无网络)。
4. 复现:`gen_fixtures.py` 重跑得到逐字节相同的 parquet + golden。

---

## 5. 风险与开放问题

| 风险 | 缓解 |
|---|---|
| QMT 偶发崩溃 / cq(长桥)缺 key | 用户确认本地已配置 QMT+cq(实盘在用),均为**一次性**拉取:QMT 偶发崩溃重试即可;`klines()` 返回 `[code,date,open,high,low,close,volume]` 直接落 parquet。fixture 一旦提交,后续测试只读本地 parquet、不再触网 |
| `process_klines` 对 DataFrame 列名/时区有隐含要求 | 直接用 `Exchange.klines()` 的原样输出当 fixture,列结构天然一致;不手工拼 DataFrame |
| golden 跨平台漂移 | 明确 golden 平台相关 + `--update-golden` 流程,写入 README |
| 孤儿测试隔离后被遗忘 | 保留目录 + 在 spec/README 标注"未来重写蓝图(Option C)" |
| P2 目录重构 import 牵连**常驻 live_monitor** | 本 spec 不做 P2;到 P2 时强制 re-export shim + 全仓(含 web/scripts)import 审计 + 重启 live_monitor 验证 |

---

## 6. 交付定义(Phase 0 Done)

- [ ] `tests/chan/` 已隔离,`pytest tests/` 不再 collection-error
- [ ] `tests/fixtures/` 含 4 个提交的 parquet(真实窗口)
- [ ] `tests/golden/` 含 4 个人工抽查过的规范化 JSON
- [ ] `tests/chan_core/test_golden_master.py` 全绿,且变异检验证明能抓漂移
- [ ] `tests/tools/gen_fixtures.py` 可幂等复现
- [ ] README 记录:平台相关性、`--update-golden` 流程、孤儿测试定位
- [ ] 更新项目记忆(纠正"910 passed";记录新安全网与路线图)

---

## 7. 后续(P1–P4,仅占位,各自再 spec)

- **P1**:语义死代码清单化 → 逐项删除 + 黄金主门控。
- **P2**:`cl_interface.py` 按领域对象拆分(呼应孤儿包的 `types.py` 设计)+ shim;再及 db/cl_utils/kcharts。
- **P3**:基于真实 fixture 做热点 profiling,延续 O(n²) 消除。
- **P4**:正确性——独立 spec,改行为前先在黄金主上确认差异是"修复"而非"回归"。
