# 删除走势段 (zsd) 与趋势段 (qsd) — 设计文档

- 日期：2026-05-06
- 范围：彻底移除走势段 (zsd) 与趋势段 (qsd) 两个级别的所有代码、配置、UI、文档、notebook 引用
- 性质：清理半残特性（`get_zsds()` / `get_qsds()` 已默认返回空，但接口、配置、UI、策略、文档仍引用）

---

## 1. 目标与动机

走势段 (zsd) 与趋势段 (qsd) 是缠论中的高级别结构（建立在线段 xd 之上）。当前实现里 `cl.py::get_zsds()` / `get_qsds()` 已直接 `return []`，意味着核心已不再产出这两类数据，但接口、配置、UI 选项、策略代码、cookbook 文档仍大量引用。这种"半残"状态对维护者与用户都是噪声。

**目标**：把 zsd / qsd 作为级别概念从代码、配置、UI、策略、文档、notebook 中彻底移除，留下干净的 `bi (笔) / xd (线段)` 两级结构 + 其上的中枢/买卖点/背驰。

**非目标**：

- **不**删除背驰类型 `pz`（盘整背驰）、`qs`（趋势背驰）—— 它们是 bi/xd 上计算的背驰**类型**，不是段级别
- **不**改动 bi、xd、bi_zs、xd_zs 的现有逻辑
- **不**删除 `_calc_qs` / `qs_bc` 等"趋势背驰"计算路径

---

## 2. 删除范围（What goes）

### 2.1 核心代码 `src/chanlun/core/`

#### `cl_interface.py`

- 删除配置枚举：
  - `Config.ZSD_BZH_NO`、`Config.ZSD_BZH_YES`
  - `Config.ZSD_QJ_DD`、`Config.ZSD_QJ_CK`、`Config.ZSD_QJ_K`
- 删除抽象方法：
  - `get_zsds()`、`get_zsd_zss()`
  - `get_qsds()`、`get_qsd_zss()`
- `ZS.zs_type` 取值集合从 `('bi','xd','zsd')` 收窄为 `('bi','xd')`
- `BC.type` 文档/取值：移除 `'zsd'`、`'qsd'`，保留 `'bi','xd','pz','qs'`

#### `cl.py`

- 删除字段 `self.zsd_zss`、`self.qsd_zss`
- 删除方法 `get_zsds()`、`get_qsds()`、`get_zsd_zss()`、`get_qsd_zss()`
- 删除属性 `zsds`、`qsds`、`type_zsd_zss`、`type_qsd_zss`
- 配置默认表中删除 `xd_bzh: Config.ZSD_BZH_YES.value` 这一行（详见 §3.1 决策）

#### `bs_point_calculator.py`

- `zs_type` 校验集合从 `('bi','xd','zsd')` 收窄为 `('bi','xd')`
- 错误消息同步更新

### 2.2 工具/配置 `src/chanlun/`

#### `cl_utils.py`

- 默认配置 `query_data_options` 中删除：
  - `zsd_qj`
  - `chart_show_zsd`、`chart_show_zsd_zs`、`chart_show_zsd_mmd`、`chart_show_zsd_bc`
  - `chart_show_qsd`、`chart_show_qsd_zs`、`chart_show_qsd_mmd`、`chart_show_qsd_bc`
- 图表数据导出：
  - 删除 `zsd_chart_data`、`zsd_zs_chart_data` 及对应的 zsd 收集 / 排序 / 输出代码块
  - 删除 qsd 同名等价物（如有）
  - 输出字典中删除 `"zsds"`、`"zsd_zss"`、`"qsds"`、`"qsd_zss"` 键
- `line_type_map`：删除 `"zsd": "走"`、`"qsd": "趋"`
- `bc_type_map`：删除 `"zsd": "ZSD"`、`"qsd": "QSD"`，保留 `"pz": "PZ"`、`"qs": "QS"`
- 多级别 mmd/bc 收集循环里删除 `zsd` / `qsd` 相关处理

#### `file_db.py`

- 缓存合并键列表中删除 `"zsds"`、`"zsd_zss"`、`"qsds"`、`"qsd_zss"`
- 旧缓存文件中如含这些键，读取时直接被丢弃（dict.get 默认行为，无需特殊兼容代码）

#### `kcharts.py`

- 删除 zsd / qsd 渲染相关分支与配置读取

### 2.3 策略 `src/chanlun/strategy/`

#### `strategy_zsd_xd_bi_1mmd.py`

**整体删除文件。** 该策略主体围绕 zsd 构建，删除 zsd 后已无意义。

#### `strategy_a_xd_trade_model.py`

**保留文件，但降级为 xd 确认（决策 4.1=b）：**

- `zsds_down_30m = [_zsd for _zsd in cd_30m.get_zsds() if ...]` → 改为 `xds_down_30m_confirm = [_xd for _xd in cd_30m.get_xds() if ...]`
- `bc_exists(["zsd", "pz", "qs"])` → `bc_exists(["xd", "pz", "qs"])`（pz/qs 保留）
- 同样改 5m 段
- 局部变量、注释、`info["day_zsd_type"]` 字段名相应改为 `day_xd_type` 或同等更通用的命名
- 策略整体的多级别交叉确认意图保留，仅把"走势段确认"替换为"线段确认"

### 2.4 Web 前端/后端 `web/chanlun_chart/cl_app/`

#### `services/chart_compute.py`

- 删除请求/响应字段中的 `zsd_qj`、`chart_show_zsd*`、`chart_show_qsd*`
- 删除返回数据中的 `zsds` / `zsd_zss` / `qsds` / `qsd_zss`

#### `blueprints/tv.py`、`blueprints/options.py`

- 删除 zsd / qsd 相关参数读取、选项写入

#### `templates/options.html`

- 删除"走势段区间"`<select name="zsd_qj">` 整个下拉块（≈ line 246–258）
- 删除走势段 / 趋势段 checkbox：
  - `chart_show_zsd`（走势段）/ `chart_show_qsd`（趋势段）
  - `chart_show_zsd_zs`（走势段中枢）/ `chart_show_qsd_zs`（趋势段中枢）
  - `chart_show_zsd_mmd`（走势段买卖点）/ `chart_show_qsd_mmd`（趋势段买卖点）
  - `chart_show_zsd_bc`（走势段背驰）/ `chart_show_qsd_bc`（趋势段背驰）
- "线段区间"行的 `title="..."` 描述里"影响走势段的特征序列计算" → 改为更准确的描述（见 §3.3）
- 配置预设保存列表（line 863 附近）中的 `"zsd_qj"` 移除

#### `static/js/charts.js`

- 删除 zsd / qsd 渲染、图例、绘制路径

#### `static/datafeeds/udf/src/history-provider.ts`

- 删除 zsd / qsd 字段读取

#### `static/datafeeds/udf/dist/bundle.js` （决策 4.2=b）

- 手工 patch：删除 `zsd` / `qsd` 相关字符串字面量与对应分支
- 修改前先备份原文件，patch 后用浏览器加载验证 datafeed 不报错
- **风险高**，单独作为最后一步执行；如手工 patch 失败，回退到 4.2=a 方案（仅改 .ts 源 + UPDATE.md 注明需重建）

### 2.5 交易脚本 `script/trader/`

5 个文件，各删除 2 行：

- `reboot_trader_currency.py` (line 34-35)
- `reboot_trader_a_stock.py` (line 629-630)
- `reboot_trader_hk_stock.py` (line 96-97)
- `reboot_trader_futures.py` (line 34-35)
- `reboot_trader_ctp.py` (line 31-32)

每处删除：

```python
"zsd_bzh": Config.ZSD_BZH_NO.value,
"zsd_qj": Config.ZSD_QJ_DD.value,
```

### 2.6 Cookbook 文档 `cookbook/docs/`（决策 3.1=a）

- `UPDATE.md`：在变更日志中**新增**一条记录"移除走势段/趋势段"，**保留**历史 zsd 相关变更条目（历史记录不动）
- `缠论买卖点和背驰规则.md`：删除涉及走势段/趋势段的章节与列表项
- `缠论配置项说明.md`：删除 `zsd_bzh`、`zsd_qj`、`chart_show_zsd*`、`chart_show_qsd*` 的说明条目
- `缠论数据对象与方法.md`：删除 `get_zsds`、`get_zsd_zss`、`get_qsds`、`get_qsd_zss` 方法说明
- `index.md`：删除"走势段"/"趋势段"目录条目（如有）
- `计算性能.md`：删除 zsd/qsd 性能数据（如有）
- `多中枢类型相同买卖点策略.md`：审阅后决定整篇删除还是仅删 zsd/qsd 段；如该策略主体依赖 zsd，则建议整篇删除并标注
- `合成自定义K线数据（分钟）.md`：删除 zsd/qsd 相关配置说明段
- `基于线段的中枢震荡策略.md`：审阅后决定（标题暗示主体是 xd 中枢，应只删 zsd/qsd 旁注）

### 2.7 Notebook `notebook/`（决策 3.2=a）

- `回测_沪深股票策略.ipynb`
- `回测_缠论参数优化.ipynb`
- `回测_期货策略.ipynb`

每个 .ipynb 用 `nbformat` 库或精确 JSON 编辑，删除：

- 含 `zsd_bzh` / `zsd_qj` / `chart_show_zsd*` / `chart_show_qsd*` 的配置 cell
- 含 `get_zsds` / `get_qsds` 的代码 cell
- 对应的 markdown 解释 cell
- **不**重跑 notebook，输出 cell 保持原样（让用户感知到旧输出已不可重现）

---

## 3. 关键决策（Why this way）

### 3.1 `Config.ZSD_BZH_*` 枚举的归宿

**问题**：`cl.py` 中 `'xd_bzh': Config.ZSD_BZH_YES.value` —— xd（线段）的标准化配置**复用**了 ZSD 枚举名。直接删除会破坏线段功能。

**决策**：

- 选项 1（采用）：把 `Config.ZSD_BZH_NO/YES` **重命名**为 `Config.XD_BZH_NO/YES`，并把 `cl.py` 默认配置改为 `'xd_bzh': Config.XD_BZH_YES.value`。同时将枚举的字符串值由 `"zsd_bzh_no"`/`"zsd_bzh_yes"` 改为 `"xd_bzh_no"`/`"xd_bzh_yes"`。
- 选项 2（不采用）：保留 `Config.ZSD_BZH_*` 命名，仅作为 xd 用，注释说明。**否决理由**：与"彻底删除 zsd"的目标矛盾，留 zsd 字面量在配置体系里是认知负担。

**用户旧配置兼容**：旧配置 `"xd_bzh": "zsd_bzh_yes"` 这一字符串值在升级后会变成无效值，落到默认分支。决策 3.3=a 已确认接受静默忽略。如有必要可在读取处加一行 `value.replace("zsd_bzh", "xd_bzh")` 兼容。**默认不做**，按 3.3=a 硬删处理。

### 3.2 `BC.type` 与 `bc_type_map` 中的 `pz` / `qs`

`pz`（盘整背驰）、`qs`（趋势背驰）在 BC 类型中**保留**。它们是 bi/xd 上计算的背驰**类型**（计算逻辑），不是 zsd/qsd 这种段**级别**。删除 zsd/qsd 后，pz/qs 仍由 bi/xd 计算路径触发。

### 3.3 options.html 中"线段区间"的 title

原 title `"线段区间计算依据，影响走势段的特征序列计算"` —— 改为 `"线段区间计算依据，影响线段的特征序列计算"`（删除"走势段"字眼，但不删整个控件）。

### 3.4 用户旧 options.json / 缓存文件

按决策 3.3=a：

- 配置 dict 读未知 key 默认行为是返回 `None`/默认值，不会爆错
- 缓存文件 (`file_db.py` pickle/parquet) 里的 `zsds`/`zsd_zss`/`qsds`/`qsd_zss` 键在合并时直接不被读取，自然丢弃
- **不**新增迁移代码，**不** bump 缓存版本号
- UPDATE.md 中提示一句"如出现配置异常，请手工删除 options.json 中的 zsd_/qsd_/chart_show_zsd_/chart_show_qsd_ 键"

### 3.5 bundle.js 手工 patch（4.2=b）

风险点：bundle.js 是 webpack/rollup 压缩产物，字符串字面量可能被 mangling。手工 patch 前置条件：

1. 先在 `history-provider.ts` 等源文件里删除 zsd/qsd 引用
2. 在 bundle.js 中 `git diff` 之前先 `cp bundle.js bundle.js.bak`
3. 仅 patch 明确未被 mangling 的字符串字面量（如 `"zsds"`、`"zsd_zss"` 这种作为对象 key 的字符串）
4. patch 后启动 web 服务，浏览器打开图表页，确认 console 无报错且图表正常加载
5. 任何不确定的点 → 回退到 4.2=a（即不 patch bundle.js，UPDATE.md 注明"需重新构建 datafeed bundle"）

---

## 4. 不删除的内容（What stays）

| 项 | 保留理由 |
|---|---|
| `Config.XD_QJ_*`、`Config.XD_BI_POHUAI_*` | 线段 (xd) 的核心配置 |
| `BC.type = "pz"`、`"qs"` | 盘整/趋势背驰类型，独立于 zsd/qsd |
| `bc_type_map["pz"]`、`["qs"]` | 同上 |
| `_calc_qs` / `qs_bc` 等趋势背驰计算 | 是 bi/xd 上的背驰**计算**，命名相近不相关 |
| `pz_bc` 盘整背驰 | 同上 |
| `qsd_zss`/`zsd_zss` 在 `cl.py` 注释里被注掉的字段 | 直接删除注释也行（无功能影响） |
| `cookbook/docs/UPDATE.md` 历史 zsd 变更条目 | 历史日志不改写 |

---

## 5. 文件清单（Final list）

### 修改（19 文件）

```
src/chanlun/core/cl_interface.py
src/chanlun/core/cl.py
src/chanlun/core/bs_point_calculator.py
src/chanlun/cl_utils.py
src/chanlun/file_db.py
src/chanlun/kcharts.py
src/chanlun/strategy/strategy_a_xd_trade_model.py
web/chanlun_chart/cl_app/services/chart_compute.py
web/chanlun_chart/cl_app/blueprints/tv.py
web/chanlun_chart/cl_app/blueprints/options.py
web/chanlun_chart/cl_app/templates/options.html
web/chanlun_chart/cl_app/static/js/charts.js
web/chanlun_chart/cl_app/static/datafeeds/udf/src/history-provider.ts
web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js   # 决策 4.2=b 手工 patch
script/trader/reboot_trader_currency.py
script/trader/reboot_trader_a_stock.py
script/trader/reboot_trader_hk_stock.py
script/trader/reboot_trader_futures.py
script/trader/reboot_trader_ctp.py
```

### 删除（1 文件）

```
src/chanlun/strategy/strategy_zsd_xd_bi_1mmd.py
```

### 文档/notebook（按内容分块改写）

```
cookbook/docs/UPDATE.md                           # 加一条 changelog
cookbook/docs/缠论买卖点和背驰规则.md             # 删 zsd/qsd 章节
cookbook/docs/缠论配置项说明.md                   # 删 zsd_*/qsd_* 配置项
cookbook/docs/缠论数据对象与方法.md               # 删 get_zsds/get_qsds 方法说明
cookbook/docs/index.md                            # 删目录条目
cookbook/docs/计算性能.md                         # 删性能数据
cookbook/docs/多中枢类型相同买卖点策略.md         # 审阅后决定整篇删/部分删
cookbook/docs/合成自定义K线数据（分钟）.md         # 删配置说明段
cookbook/docs/基于线段的中枢震荡策略.md           # 删旁注
notebook/回测_沪深股票策略.ipynb                  # 删 zsd/qsd cell
notebook/回测_缠论参数优化.ipynb                  # 删 zsd/qsd cell
notebook/回测_期货策略.ipynb                      # 删 zsd/qsd cell
```

---

## 6. 验收标准

### 6.1 静态检查

- `grep -rn -E "zsd|qsd|走势段|趋势段|ZSD|QSD" src/ web/ script/` 仅在以下场景有命中：
  - 注释里历史变更说明
  - `pz`/`qs` 字符串作为子串巧合（如 `"position_qsd"` 这种应不存在）
  - `cookbook/docs/UPDATE.md` 历史 changelog
- `ruff check` 通过
- `python -c "from chanlun.core.cl import CL; ..."` 不抛 ImportError / AttributeError

### 6.2 动态验证

- 单元/集成测试 `pytest` 全绿（与本次删除相关的测试如有需同步更新）
- Web 端 `python web/chanlun_chart/main.py`（或等价启动命令）启动成功
- 浏览器打开 options 页：无 zsd/qsd 选项；保存配置不报错
- 浏览器打开图表页：图表加载成功，console 无 4xx/5xx，无 `chart_show_zsd` 等未定义引用
- 主要策略回测脚本能加载（不需要跑完整回测）：
  - `script/trader/reboot_trader_a_stock.py` import 无报错
  - `strategy_a_xd_trade_model.py` import 无报错

### 6.3 用户兼容性

- 旧 `options.json` 中残留 `chart_show_zsd*` / `chart_show_qsd*` / `zsd_qj` 等键 → 启动时不报错（dict.get 静默忽略）
- 旧 file_db 缓存中残留 `zsds` / `zsd_zss` / `qsds` / `qsd_zss` 键 → 读取时不报错

---

## 7. 风险与回滚

### 7.1 主要风险

| 风险 | 缓解 |
|---|---|
| `Config.ZSD_BZH_*` 重命名遗漏，xd 标准化失效 | 重命名后用 `grep ZSD_BZH` 确认仅命中 enum 定义；启动 web 跑一遍线段计算 |
| `strategy_a_xd_trade_model.py` 降级后语义偏差 | 降级前后跑同一段历史数据回测，确认信号数量级未爆炸/归零 |
| bundle.js 手工 patch 破坏前端 | 4.2=b 步骤前先 cp 备份；patch 后浏览器逐页验证；失败则回退到 4.2=a |
| notebook 编辑破坏 .ipynb JSON 结构 | 用 `nbformat` 库做读写，不用纯字符串替换；改后 `jupyter nbconvert --to notebook --execute` 验证 |
| 旧用户 options.json 落到默认分支后行为变化 | UPDATE.md 注明清理建议；启动时 log warn 一次列出残留 key（可选） |

### 7.2 回滚方案

- 按提交粒度回滚：本次工作建议拆 5 个 commit（详见后续 plan），任意 commit 可独立 revert
- bundle.js 单独保留 .bak 备份直到验收通过

---

## 8. 提交粒度建议（实施时由 plan 阶段细化）

预计 5 个原子 commit：

1. **核心层**：`cl_interface.py` + `cl.py` + `bs_point_calculator.py` + `Config.ZSD_BZH_*` 重命名（含 cl.py 默认配置同步）
2. **工具/缓存**：`cl_utils.py` + `file_db.py` + `kcharts.py`
3. **策略**：删除 `strategy_zsd_xd_bi_1mmd.py` + 改写 `strategy_a_xd_trade_model.py`
4. **Web/UI/脚本**：`web/chanlun_chart/**` + `script/trader/**`（含 bundle.js 手工 patch，作为本 commit 末尾步骤；patch 失败则单独退化 commit）
5. **文档/notebook**：`cookbook/docs/**` + `notebook/*.ipynb` + `UPDATE.md` changelog

---

## 9. 不在范围内（Out of scope）

- 不重构 bi、xd、bi_zs、xd_zs 任何内部逻辑
- 不引入新功能替代 zsd/qsd
- 不修改第三方包（`.codex_debug_venv/` 下的任何 tqsdk / akshare 字符串巧合命中）
- 不重新构建 web bundle（仅手工 patch 现有产物，4.2=b）
- 不改写历史 git commit message
