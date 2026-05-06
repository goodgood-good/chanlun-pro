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
