# 设计：板块概念面板"标的列表化"升级

- 日期：2026-05-07
- 范围：仅前端就地改造 `index.html` 主图表页右侧"板块概念"折叠面板
- 触点：`web/chanlun_chart/cl_app/static/js/bkgn.js`、`web/chanlun_chart/cl_app/templates/index.html`
- 后端：不改动

## 1. 背景

主图表页右侧 sidebar 折叠面板"板块概念"（仅 A 股可见）当前实现：

- 顶部 `xm-select` 单选下拉（`radio: true`、自带 `filterable`），列出 ~600 项行业/概念，名称形如 `行业:白酒` / `概念:新能源`
- 选中后下方 `bkgn_table` 渲染该板块成员股（layui table，已分页 20/页，列：代码、名称）
- 点击成员股票行调 `change_chart_ticker` 切主图表

用户反馈：体验不够"像标的列表"。`symbols.html` 标的列表页（独立页面，路由 `/symbols`）的体验范式是：**搜索框 + 分页 table + 重置按钮**，希望把这个折叠面板也改成那个样子。

## 2. 目标与非目标

### 目标

- 板块层和成员股票层都改造为 "搜索框 + 分页 layui table" 形式
- 保持现有点击行为（板块行 → 加载成员；股票行 → 切换主图表）
- 不动后端 API 接口和返回结构
- 不破坏既有的"非 A 股市场隐藏整个面板"行为

### 非目标

- 不引入拼音首字母搜索（前后端都不动）
- 不做"行业 / 概念"分类 tab 或筛选下拉（搜"行业"或"概念"前缀即可达到等价效果）
- 不新增独立路由 / 独立页面
- 不做数据持久化（用户跨会话的选中状态不保存）

## 3. 架构

纯前端就地改造，**单一职责的两个组件**叠在原折叠面板里：

```
#collapse-bkgn (现有 layui-colla-item)
└── 板块层组件
│   ├── 搜索栏：input[关键字] + 查询按钮 + 重置按钮
│   └── #bkgn_table       (layui table, 1 列, 分页 20, 高度 ~280px)
└── 成员股票层组件
    ├── 搜索栏：input[关键字] + 查询按钮 + 重置按钮
    └── #bkgn_stock_table (layui table, 2 列, 分页 20, 高度 ~280px)
```

两层组件之间唯一的耦合点：板块层选中某行后向成员层注入 `(type, code)`，触发成员层加载并渲染。其它行为各自独立。

`xm-select` 完全移除（连带 `xmSelectIns` 状态、依赖代码删除）。

## 4. 数据流

```
[A 股展开折叠面板]
       │
       ▼
GET /a/bkgn_list  ──→  全量缓存到 BKGN.allBkgnList (~600 项)
                        │
                        ▼
                 渲染 #bkgn_table (默认显示全部, 分页)
                        │
       ┌────────────────┴────────────────┐
       │ 上层搜索框输入                   │ 上层点击行
       ▼                                  ▼
   按 bkgn_name 子串前端过滤      POST /a/bkgn_codes
   table.reload(filtered)              │
                                  按 (type|code) 缓存 stocks
                                        │
                                        ▼
                          渲染 #bkgn_stock_table
                                        │
                       ┌────────────────┴────────────────┐
                       │ 下层搜索框输入                   │ 下层点击行
                       ▼                                  ▼
                  按 code/name 子串前端过滤      change_chart_ticker(market, code)
                  table.reload(filtered)         + ai_code 联动
```

**前端缓存策略**：

- `allBkgnList`：模块加载时填充一次，永驻；首次 `GET /a/bkgn_list` 后写入
- `stocksCache`：`Map<"hy|白酒", { stocks }>`，重复点击同一板块复用，避免重复 POST
- 缓存键采用 `${type}|${code}`，与现有后端约定一致

## 5. 接口约定（不变）

复用现有：

| 接口 | 方法 | 入参 | 出参 |
|---|---|---|---|
| `/a/bkgn_list` | GET | — | `{ code: 0, data: [{ type: "hy"\|"gn", bkgn_name: "行业:白酒", bkgn_code: "白酒" }, ...], count }` |
| `/a/bkgn_codes` | POST | `bkgn_type`, `bkgn_code` | `{ code: 0, data: { "000858": { name: "五粮液", ... }, ... } }` |

## 6. UI 细节

- 两层搜索栏布局：`input.layui-input` + `查询`（`layui-btn layui-bg-red`）+ `重置`（`layui-btn-primary`），与 `symbols.html` 视觉对齐
- 上层 table：单列"板块名"，列宽 100%；保留 `行业:` / `概念:` 前缀以示区分
- 下层 table：列 1"代码"（48%），列 2"名称"（48%），与现有 bkgn_table 一致
- 两层 table 各自固定高度 ~280px（具体值实施时按视觉调），超出由 layui 自带分页或滚动处理
- 选中态：layui table `setRowChecked` 给当前选中板块和当前选中股票各自高亮；切换板块时清掉成员表选中态
- 搜索触发：input `keyup`（带 200ms 防抖）+ 显式"查询"按钮 + Enter 键回车均触发；"重置"清空 input 并恢复全量列表

## 7. 错误与边界

- `GET /a/bkgn_list` 失败：上层 table 空，`layer.msg("获取板块概念失败")`（沿用现状）
- `POST /a/bkgn_codes` 失败：下层 table 空，`layer.msg("获取股票列表失败")`（沿用现状）
- 上层搜索无结果：layui table 自带 "无数据" 提示
- 下层搜索无结果：同上
- 切到非 A 股市场：保持 `$('#collapse-bkgn').hide()`（沿用现状）
- 切回 A 股：面板恢复可见，已缓存的 `allBkgnList` 和上次选中状态保留，**不重新拉接口**
- 重复点击同一板块：命中 `stocksCache`，不发 POST

## 8. 文件变动清单

| 文件 | 类型 | 主要改动 |
|---|---|---|
| `web/chanlun_chart/cl_app/static/js/bkgn.js` | 重写 | 删除 `xm-select` 相关；新增两层组件初始化、前端过滤、缓存、搜索栏事件绑定 |
| `web/chanlun_chart/cl_app/templates/index.html` | 微改 | 折叠面板内删 `#bkgn_xm_select`、增两组搜索栏 DOM、保留 `#bkgn_table`、新增 `#bkgn_stock_table` |

后端零改动。

## 9. 验证清单（手动）

部署后在主图表页（A 股）依次确认：

1. 展开"板块概念"折叠面板 → 上层 table 显示板块前 20 项，分页可翻
2. 上层搜"白酒" → 只剩匹配 `行业:白酒` 等
3. 上层搜"概念" → 全是 `概念:xxx` 项
4. 点击"行业:白酒" → 下层 table 加载成员，可分页
5. 下层搜"000858" 或 "五粮液" → 过滤命中
6. 下层点击某行 → 主图表切换到该股票，`#ai_code` 同步
7. 重复点击同一板块行 → Network 中无重复 POST `/a/bkgn_codes`（命中前端缓存）
8. 切到非 A 股 → 面板隐藏
9. 切回 A 股 → 面板状态恢复，无报错

## 10. 实施风险

- `layui-table` 的 `data` 模式 reload 行为差异：需要 `table.reloadData(id, { data: filtered })` 而非 `table.render`，否则会导致状态/分页错乱。实施时需注意。
- 如折叠面板宽度 < 280px，table 会出现横向滚动条；可在样式上 `overflow-x: hidden` 避免视觉割裂
- `xm-select` 移除后需确认 index.html 没有其它地方引用 `#bkgn_xm_select`（已 grep，无）

## 11. 后续可能扩展（不在本次范围）

- 拼音首字母搜索（需后端 enrich 字段）
- 行业 / 概念分类 tab
- 板块成员数量列、当日涨跌幅列
- 跨会话持久化最近选中板块（`localStorage`）

— 完 —
