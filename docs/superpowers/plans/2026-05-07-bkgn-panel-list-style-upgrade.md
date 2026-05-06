# 板块概念面板"标的列表化"升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `index.html` 主图表页右侧的"板块概念"折叠面板从 `xm-select 下拉 + 单层表格` 升级为 `两层独立的"搜索框 + 分页 layui table"`，体验对齐 `symbols.html` 标的列表页。

**Architecture:** 纯前端就地改造。后端 `/a/bkgn_list` 与 `/a/bkgn_codes` 不变。前端在 `bkgn.js` 内部维护两份缓存（板块全量列表 + 板块成员股票分板块缓存），两层 table 各自的搜索框走前端子串过滤。`xm-select` 完全移除。

**Tech Stack:** layui table、jQuery、原项目已有的 `change_chart_ticker` 全局函数；不引入新依赖。

**Spec:** `docs/superpowers/specs/2026-05-07-bkgn-panel-list-style-upgrade-design.md`

---

## File Structure

| 路径 | 类型 | 职责 |
|---|---|---|
| `web/chanlun_chart/cl_app/templates/index.html` | 修改 | 折叠面板内 DOM 重排：删 `#bkgn_xm_select`、新增两组搜索栏 DOM、新增 `#bkgn_stock_table` 容器 |
| `web/chanlun_chart/cl_app/static/js/bkgn.js` | 重写 | 移除 `xm-select` 相关；实现两层 table 渲染、前端过滤、缓存、搜索栏事件绑定 |

后端无改动。

**测试策略**：项目无前端单测体系（layui+jQuery 老栈），采用 `手动验证清单` 替代 TDD。每个 Task 完成后跑该 Task 对应的验证步骤；Task 4 跑完整验证清单。

---

## Task 1：重排 index.html 折叠面板 DOM

**Files:**
- Modify: `web/chanlun_chart/cl_app/templates/index.html` (折叠面板内部，约 line 351–375)

- [ ] **Step 1：定位现状代码**

Run:
```bash
grep -n 'collapse-bkgn\|bkgn_xm_select\|bkgn_table' web/chanlun_chart/cl_app/templates/index.html
```
Expected：定位到 `#collapse-bkgn` 折叠区起止行（约 351 起）、`#bkgn_xm_select`（约 362）、`#bkgn_table`（约 369）。

- [ ] **Step 2：替换折叠面板内 DOM**

把当前折叠区内容（line 355–374 的 `<div class="layui-colla-content">…</div>`）整体替换为下列 DOM。注意保留外层 `<div class="layui-colla-item" id="collapse-bkgn">` 与 `<div class="layui-colla-title" data-ca-title="板块概念">板块概念</div>`，仅替换内层内容容器：

```html
              <div class="layui-colla-content" style="padding: 0">
                <!-- 上层：板块选择 -->
                <div style="padding: 10px 10px 0 10px;">
                  <div class="layui-form-item" style="margin-bottom: 6px;">
                    <div class="layui-input-inline" style="width: 60%;">
                      <input
                        type="text"
                        id="bkgn_search"
                        class="layui-input"
                        placeholder="搜索板块名（行业/概念）"
                        autocomplete="off" />
                    </div>
                    <button type="button" id="bkgn_search_btn"
                      class="layui-btn layui-bg-red layui-btn-sm">查询</button>
                    <button type="button" id="bkgn_reset_btn"
                      class="layui-btn layui-btn-primary layui-btn-sm">重置</button>
                  </div>
                  <table
                    class="layui-hide"
                    id="bkgn_table"
                    lay-filter="bkgn_table"
                    style="width: 100%;"></table>
                </div>
                <hr style="margin: 8px 0;" />
                <!-- 下层：板块成员股票 -->
                <div style="padding: 0 10px 10px 10px;">
                  <div class="layui-form-item" style="margin-bottom: 6px;">
                    <div class="layui-input-inline" style="width: 60%;">
                      <input
                        type="text"
                        id="bkgn_stock_search"
                        class="layui-input"
                        placeholder="搜索代码 / 名称"
                        autocomplete="off" />
                    </div>
                    <button type="button" id="bkgn_stock_search_btn"
                      class="layui-btn layui-bg-red layui-btn-sm">查询</button>
                    <button type="button" id="bkgn_stock_reset_btn"
                      class="layui-btn layui-btn-primary layui-btn-sm">重置</button>
                  </div>
                  <table
                    class="layui-hide"
                    id="bkgn_stock_table"
                    lay-filter="bkgn_stock_table"
                    style="width: 100%;"></table>
                </div>
                <hr class="layui-border-red" />
              </div>
```

要点：
- **删除** 原 `#bkgn_xm_select` 节点（不再使用 xm-select）
- **保留** `#bkgn_table` 的 id 与 `lay-filter`，以便 bkgn.js 沿用
- **新增** `#bkgn_stock_table`（成员股票表）+ 各自的 search/btn 元素

- [ ] **Step 3：检查模板没有破坏**

Run:
```bash
grep -n 'bkgn_xm_select\|#bkgn_table\|#bkgn_stock_table\|bkgn_search\|bkgn_stock_search' web/chanlun_chart/cl_app/templates/index.html
```
Expected：
- `bkgn_xm_select` 命中 0 次（已删除）
- `bkgn_search` 命中 ≥ 3 次（input + 2 个 button）
- `bkgn_stock_search` 命中 ≥ 3 次
- `#bkgn_table`、`#bkgn_stock_table` 各 1 次（在 DOM 处）

- [ ] **Step 4：手动验证渲染**

启动 web 服务，打开主图表页（A 股），展开"板块概念"折叠面板。
Expected：
- 看到上层一个搜索框 + 查询/重置按钮（下方空 table 占位区）
- 看到下层一个搜索框 + 查询/重置按钮（下方空 table 占位区）
- **此时点击搜索/查询不会有效果**（bkgn.js 还没改），属于预期

- [ ] **Step 5：提交**

```bash
git add web/chanlun_chart/cl_app/templates/index.html
git commit -m "$(cat <<'EOF'
feat(bkgn): 重排折叠面板 DOM 为两层"搜索栏 + table"骨架

- 删除 xm-select 容器 (#bkgn_xm_select)
- 新增上层板块搜索栏 + 保留 #bkgn_table
- 新增下层成员股票搜索栏 + #bkgn_stock_table
- 仅 DOM 结构调整，行为待 bkgn.js 接入

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2：重写 bkgn.js（核心逻辑）

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/js/bkgn.js`（整个文件替换为下面的实现）

- [ ] **Step 1：备份与替换**

整体替换 `bkgn.js` 为以下内容（IIFE 模块结构与原文件一致，对外仅暴露 `init_bkgn_opts`，与 `index.html` 现有调用 `BKGN.init_bkgn_opts()` 兼容）：

```javascript
// 板块概念 JS 封装：两层"搜索 + 分页 table"模式
// 上层：板块（行业/概念）选择；下层：板块成员股票
// 搜索均为前端子串过滤，不调后端。后端仅 /a/bkgn_list 与 /a/bkgn_codes
var BKGN = (function () {
  // ---- 状态 ----
  var allBkgnList = [];         // 板块全量缓存（首次 GET /a/bkgn_list 后永驻）
  var stocksCache = new Map();  // 成员股票按板块缓存：key="hy|白酒"
  var currentBkgnKey = null;    // 当前选中板块的 key（用于翻页后恢复高亮）
  var currentStockData = [];    // 下层当前完整数据（搜索过滤的源）
  var bkgnRowClickBound = false;
  var stockRowClickBound = false;
  var searchHandlersBound = false;
  var SEARCH_DEBOUNCE_MS = 200;
  var bkgnSearchTimer = null;
  var stockSearchTimer = null;
  var TABLE_HEIGHT = 280;

  // ---- 入口 ----
  function init_bkgn_opts() {
    bind_search_handlers();
    fetch_bkgn_list();
  }

  // ---- 板块层 ----
  function fetch_bkgn_list() {
    if (allBkgnList.length > 0) {
      // 二次进入：直接复用缓存
      render_bkgn_table(allBkgnList);
      return;
    }
    $.get("/a/bkgn_list", function (res) {
      if (res && res.code === 0) {
        allBkgnList = res.data || [];
        render_bkgn_table(allBkgnList);
      } else {
        layer.msg("获取板块概念失败");
      }
    });
  }

  function render_bkgn_table(list) {
    var tableData = list.map(function (item) {
      return {
        bkgn_name: item.bkgn_name,
        bkgn_code: item.bkgn_code,
        type: item.type,
      };
    });
    layui.table.render({
      elem: "#bkgn_table",
      data: tableData,
      cols: [[
        { field: "bkgn_name", title: "板块名" }
      ]],
      page: true,
      limit: 20,
      skin: "row",
      even: true,
      height: TABLE_HEIGHT,
      done: function () {
        if (currentBkgnKey) restore_bkgn_highlight();
      },
    });
    if (!bkgnRowClickBound) {
      layui.table.on("row(bkgn_table)", on_bkgn_row_click);
      bkgnRowClickBound = true;
    }
  }

  function on_bkgn_row_click(obj) {
    var data = obj.data;
    currentBkgnKey = data.type + "|" + data.bkgn_code;
    layui.table.setRowChecked("bkgn_table", { index: "all", checked: false });
    layui.table.setRowChecked("bkgn_table", { index: obj.index });
    load_bkgn_codes(data.type, data.bkgn_code);
  }

  function restore_bkgn_highlight() {
    // 翻页或搜索 reload 后，依据 currentBkgnKey 恢复行高亮
    var cache = layui.table.cache && layui.table.cache.bkgn_table;
    if (!cache) return;
    for (var i = 0; i < cache.length; i++) {
      if ((cache[i].type + "|" + cache[i].bkgn_code) === currentBkgnKey) {
        layui.table.setRowChecked("bkgn_table", { index: i });
        break;
      }
    }
  }

  // ---- 成员股票层 ----
  function load_bkgn_codes(type, code) {
    var key = type + "|" + code;
    if (stocksCache.has(key)) {
      render_bkgn_stock_table(stocksCache.get(key));
      return;
    }
    layer.load(1);
    $.post(
      "/a/bkgn_codes",
      { bkgn_type: type, bkgn_code: code },
      function (res) {
        layer.closeAll("loading");
        if (res && res.code === 0) {
          var stocks = res.data || {};
          stocksCache.set(key, stocks);
          render_bkgn_stock_table(stocks);
        } else {
          layer.msg("获取股票列表失败");
        }
      }
    );
  }

  function stocks_to_list(stocks) {
    var list = [];
    for (var code in stocks) {
      if (stocks.hasOwnProperty(code)) {
        list.push({ code: code, name: (stocks[code] && stocks[code].name) || "" });
      }
    }
    return list;
  }

  function render_bkgn_stock_table(stocks) {
    currentStockData = stocks_to_list(stocks);
    $("#bkgn_stock_search").val(""); // 切换板块时清空下层搜索框
    do_render_stock_table(currentStockData);
    $("#bkgn_stock_table").show();
  }

  function do_render_stock_table(data) {
    layui.table.render({
      elem: "#bkgn_stock_table",
      data: data,
      cols: [[
        { field: "code", title: "代码", width: "48%" },
        { field: "name", title: "名称", width: "48%" },
      ]],
      page: true,
      limit: 20,
      skin: "row",
      even: true,
      height: TABLE_HEIGHT,
    });
    if (!stockRowClickBound) {
      layui.table.on("row(bkgn_stock_table)", on_stock_row_click);
      stockRowClickBound = true;
    }
  }

  function on_stock_row_click(obj) {
    var data = obj.data;
    change_chart_ticker(Utils.get_market(), data.code);
    $("#ai_code").val(data.code);
    layui.table.setRowChecked("bkgn_stock_table", { index: "all", checked: false });
    layui.table.setRowChecked("bkgn_stock_table", { index: obj.index });
  }

  // ---- 搜索栏 ----
  function bind_search_handlers() {
    if (searchHandlersBound) return;

    // 板块搜索
    $("#bkgn_search").on("keyup", function (e) {
      var kw = $(this).val().trim();
      clearTimeout(bkgnSearchTimer);
      if (e.key === "Enter") {
        do_bkgn_search(kw);
      } else {
        bkgnSearchTimer = setTimeout(function () { do_bkgn_search(kw); }, SEARCH_DEBOUNCE_MS);
      }
    });
    $("#bkgn_search_btn").on("click", function () {
      do_bkgn_search($("#bkgn_search").val().trim());
    });
    $("#bkgn_reset_btn").on("click", function () {
      $("#bkgn_search").val("");
      do_bkgn_search("");
    });

    // 成员股票搜索
    $("#bkgn_stock_search").on("keyup", function (e) {
      var kw = $(this).val().trim();
      clearTimeout(stockSearchTimer);
      if (e.key === "Enter") {
        do_stock_search(kw);
      } else {
        stockSearchTimer = setTimeout(function () { do_stock_search(kw); }, SEARCH_DEBOUNCE_MS);
      }
    });
    $("#bkgn_stock_search_btn").on("click", function () {
      do_stock_search($("#bkgn_stock_search").val().trim());
    });
    $("#bkgn_stock_reset_btn").on("click", function () {
      $("#bkgn_stock_search").val("");
      do_stock_search("");
    });

    searchHandlersBound = true;
  }

  function do_bkgn_search(kw) {
    var filtered = !kw
      ? allBkgnList
      : allBkgnList.filter(function (item) {
          return item.bkgn_name && item.bkgn_name.indexOf(kw) >= 0;
        });
    render_bkgn_table(filtered);
  }

  function do_stock_search(kw) {
    var filtered = !kw
      ? currentStockData
      : currentStockData.filter(function (item) {
          return (
            (item.code && item.code.indexOf(kw) >= 0) ||
            (item.name && item.name.indexOf(kw) >= 0)
          );
        });
    do_render_stock_table(filtered);
  }

  return {
    init_bkgn_opts: init_bkgn_opts,
  };
})();
```

要点：
- **不再依赖 xm-select**：模块内已无 `xmSelect` 引用。`xm-select.js` 文件其它地方可能在用，**不删 script 引用**
- **仅暴露 `init_bkgn_opts`**：与 `index.html` 现有调用兼容
- **行点击事件 + 搜索事件均只绑定一次**（用 `*Bound` 标志位防重复绑定，避免每次 render 后 layui table 内部重渲染时事件叠加）
- **搜索带 200ms 防抖**，回车键即时触发
- **行高亮恢复**：翻页或搜索 reload 后，根据 `currentBkgnKey` 在新 cache 里找回选中行（仅板块层；股票层切换板块时本来就清掉了选中态）

- [ ] **Step 2：本地语法/lint 检查**

Node 没集成进项目（前端是 layui+jQuery 静态文件），所以不跑 ESLint。打开浏览器 DevTools Console，加载主图表页 → 切到 A 股 → 展开折叠面板，**Console 不应有 JS 报错**。

- [ ] **Step 3：手动验证 — 板块层**

依次做：
1. 折叠面板"板块概念"展开 → 上层 table 显示前 20 项板块（行业/概念混排，名称带前缀）
2. 翻到第 2 页 → 仍能看到板块项；翻页流畅
3. 上层搜索框输入 "白酒" → 只剩匹配项；按"重置"恢复全量
4. 输入 "概念" → 全是"概念:xxx"项；按 Enter 立即触发；输入空格清空再 → 恢复全量

Expected：所有 4 步均符合，DevTools Console 无错。

- [ ] **Step 4：手动验证 — 成员股票层**

接着上一步：
1. 点击上层任意板块行（例如"行业:白酒"）→ 下层 table 加载该板块成员
2. 下层 input 输入 "000858" 或 "五粮液" → 过滤命中
3. 点重置 → 全量恢复
4. 点击下层任意行 → 主图表切换到该股票，`#ai_code` 同步赋值
5. 重复点击同一上层板块行 → DevTools Network 中**不应有重复 POST `/a/bkgn_codes`**（命中前端缓存）

Expected：5 步均符合。

- [ ] **Step 5：手动验证 — 选中态/翻页**

1. 上层选了某板块（高亮）→ 翻到下一页 → 翻回原页 → **该板块仍处于高亮态**（依靠 `currentBkgnKey` 恢复）
2. 上层切换到另一板块 → 下层 search 框被清空、下层 table 被替换为新成员

Expected：2 步均符合。

- [ ] **Step 6：手动验证 — 市场切换**

1. 切到非 A 股（如港股）→ 整个 `#collapse-bkgn` 隐藏（沿用现有逻辑，由 `index.html` 控制）
2. 切回 A 股 → 面板恢复可见，已加载过的板块/成员状态保留

Expected：2 步均符合。

- [ ] **Step 7：提交**

```bash
git add web/chanlun_chart/cl_app/static/js/bkgn.js
git commit -m "$(cat <<'EOF'
feat(bkgn): 重写 bkgn.js 为两层"搜索 + 分页 table"模式

- 移除 xm-select 依赖
- 板块全量一次拉取，前端按板块名子串搜索
- 成员股票按板块前端缓存，重复点击不发 POST
- 成员股票按代码 / 名称子串前端搜索
- 行点击 + 搜索事件均防重复绑定
- 翻页/搜索后依靠 currentBkgnKey 恢复板块行高亮

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3：联调微调与最终验证

**Files:**
- Modify (按需): `web/chanlun_chart/cl_app/templates/index.html` — 仅在视觉细节上调整高度/边距
- Modify (按需): `web/chanlun_chart/cl_app/static/js/bkgn.js` — 仅在 layui table 行为有出入时微调

- [ ] **Step 1：跑完整验证清单**

依 spec §9 全量过一遍：

1. ✅ A 股展开面板 → 上层 table 显示板块前 20 项，分页可翻
2. ✅ 上层搜"白酒" → 只剩匹配项
3. ✅ 上层搜"概念" → 全是 `概念:xxx` 项
4. ✅ 点某板块 → 下层 table 加载成员
5. ✅ 下层搜"000858" 或 "五粮液" → 过滤命中
6. ✅ 下层点行 → 主图表切换到该股票，`#ai_code` 同步
7. ✅ 重复点同一板块行 → Network 无重复 POST `/a/bkgn_codes`
8. ✅ 切到非 A 股 → 面板隐藏
9. ✅ 切回 A 股 → 面板状态恢复，无报错

- [ ] **Step 2：视觉与可用性微调（按实际情况）**

可能的微调（仅在出现问题时改）：
- 折叠面板宽度过窄导致按钮被挤换行 → 把 `width: 60%` 调整为 `width: 55%` 或把按钮改为 `layui-btn-xs`
- 两层 table 同屏纵向太长 → 把 `TABLE_HEIGHT` 从 280 调到 240
- 翻页后高亮恢复闪烁 → 把 `restore_bkgn_highlight` 包入 `setTimeout(..., 0)` 异步

每次微调都跑一次相关步骤验证。

- [ ] **Step 3：DevTools Console 终检**

跑完 Step 1 全部 9 步，DevTools Console **不应有任何 error/warning**（layui table 自身的 deprecation warning 除外）。

- [ ] **Step 4：提交（仅在 Step 2 有改动时）**

```bash
git add -p   # 选择性提交
git commit -m "$(cat <<'EOF'
chore(bkgn): 微调面板视觉与高亮恢复时序

- <根据实际改动写明，例如：调整 table 高度、按钮尺寸>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

如 Step 2 无改动，跳过此 step。

---

## Self-Review

| 检查项 | 结果 |
|---|---|
| Spec §1 背景 / §2 目标 | Task 1（DOM）+ Task 2（逻辑）覆盖 |
| Spec §3 架构（两层组件、移除 xm-select） | Task 1 删 `#bkgn_xm_select`、Task 2 移除模块内 xm-select 引用 |
| Spec §4 数据流（5 步） | Task 2 Step 1 代码块完整实现 5 步 |
| Spec §5 接口约定 | 不变；Task 2 调用 `/a/bkgn_list` 与 `/a/bkgn_codes` 与现有约定一致 |
| Spec §6 UI 细节（搜索栏布局、列结构、高度、防抖、Enter） | Task 1 DOM + Task 2 代码均覆盖 |
| Spec §7 错误与边界（API 失败、搜索无结果、市场切换） | Task 2 `layer.msg` + Task 3 验证 Step 1.8/1.9 覆盖 |
| Spec §8 文件变动 | Task 1 / Task 2 两文件 |
| Spec §9 验证清单 | Task 3 Step 1 完整复刻 |
| Spec §10 实施风险（layui reload 行为、宽度溢出） | Task 2 用 `table.render` 替代 reload；Task 3 Step 2 视觉微调兜底 |
| Spec §11 后续扩展 | 明确不做，无对应 task |
| Placeholder 扫描 | 无 TBD/TODO；微调命令模板里 `<根据实际改动写明>` 是合理占位说明 |
| 类型/方法名一致性 | `init_bkgn_opts`、`render_bkgn_table`、`render_bkgn_stock_table`、`load_bkgn_codes`、`do_bkgn_search`、`do_stock_search` 在 Task 2 内部前后一致 |

— 完 —
