// 板块概念面板：两层"搜索 + 分页 table"模式（A 股专用）
// 搜索为前端子串过滤；后端仅 GET /a/bkgn_list 与 POST /a/bkgn_codes
var BKGN = (function () {
  var allBkgnList = [];         // 板块全量缓存（首次拉取后永驻）
  var stocksCache = new Map();  // 成员股票按板块缓存：key="hy|白酒"
  var currentBkgnKey = null;    // 用于翻页 / 搜索 reload 后恢复高亮
  var currentStockData = [];    // 下层完整数据，前端过滤的源
  var visibleStocks = [];       // 当前下层 table 渲染中的数据（搜索过滤后）
  var currentStockIndex = -1;   // 键盘导航当前选中行（基于 visibleStocks）
  var stockRequestGeneration = 0;
  var bkgnRowClickBound = false;
  var stockRowClickBound = false;
  var searchHandlersBound = false;
  var SEARCH_DEBOUNCE_MS = 200;
  var TABLE_HEIGHT = 280;
  function appAjax(options) {
    var requestOptions = Object.assign({ timeout: 10000 }, options || {});
    if (window.AppRequest && typeof window.AppRequest.ajax === "function") {
      return window.AppRequest.ajax(requestOptions);
    }
    return $.ajax(requestOptions);
  }

  function init_bkgn_opts() {
    bind_search_handlers();
    bind_layer_toggle();
    fetch_bkgn_list();
  }

  // 板块层标题点击折叠 / 展开 body
  function bind_layer_toggle() {
    $("#bkgn_layer_toggle").off("click.bkgn").on("click.bkgn", function () {
      $(this).toggleClass("is-collapsed");
      $("#bkgn_layer_body").slideToggle(120);
    });
  }

  function fetch_bkgn_list() {
    if (allBkgnList.length > 0) {
      render_bkgn_table(allBkgnList);
      return;
    }
    appAjax({
      url: "/a/bkgn_list",
      type: "GET",
      dataType: "json",
    }).done(function (res) {
      if (res && res.code === 0) {
        allBkgnList = res.data || [];
        render_bkgn_table(allBkgnList);
      } else {
        layer.msg("板块数据加载失败，请稍后重试");
      }
    }).fail(function () {
      layer.msg("板块数据加载失败，请稍后重试");
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
      cols: [[{ field: "bkgn_name", title: "板块名称" }]],
      text: { none: "没有匹配的板块" },
      page: false,
      size: "sm",
      skin: "row",
      even: true,
      height: TABLE_HEIGHT,
      done: function () {
        $("#bkgn_total_tip").text("共 " + tableData.length + " 个板块");
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
    var cache = layui.table.cache && layui.table.cache.bkgn_table;
    if (!cache) return;
    for (var i = 0; i < cache.length; i++) {
      if ((cache[i].type + "|" + cache[i].bkgn_code) === currentBkgnKey) {
        layui.table.setRowChecked("bkgn_table", { index: i });
        break;
      }
    }
  }

  function load_bkgn_codes(type, code) {
    var key = type + "|" + code;
    var requestGeneration = ++stockRequestGeneration;
    if (stocksCache.has(key)) {
      if (key === currentBkgnKey) {
        render_bkgn_stock_table(stocksCache.get(key));
      }
      return;
    }
    var loadingIndex = layer.load(1);
    appAjax({
      url: "/a/bkgn_codes",
      type: "POST",
      data: { bkgn_type: type, bkgn_code: code },
      dataType: "json",
    }).done(function (res) {
      if (res && res.code === 0) {
        var stocks = res.data || {};
        stocksCache.set(key, stocks);
        if (requestGeneration === stockRequestGeneration && key === currentBkgnKey) {
          render_bkgn_stock_table(stocks);
        }
      } else if (requestGeneration === stockRequestGeneration) {
        layer.msg("成分股加载失败，请稍后重试");
      }
    }).fail(function () {
      if (requestGeneration === stockRequestGeneration) {
        layer.msg("成分股加载失败，请稍后重试");
      }
    }).always(function () {
      layer.close(loadingIndex);
    });
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

  function stock_code_template(code) {
    var span = document.createElement("span");
    span.className = "symbol-code-link";
    span.textContent = String(code == null ? "" : code);
    return span.outerHTML;
  }

  function render_bkgn_stock_table(stocks) {
    currentStockData = stocks_to_list(stocks);
    $("#bkgn_stock_search").val(""); // 切换板块时清空下层搜索
    do_render_stock_table(currentStockData, { focus: true });
  }

  function do_render_stock_table(data, opts) {
    opts = opts || {};
    visibleStocks = data;
    currentStockIndex = -1;
    layui.table.render({
      elem: "#bkgn_stock_table",
      data: data,
      cols: [[
        {
          field: "code",
          title: "证券代码",
          width: "48%",
          templet: function (d) {
            return stock_code_template(d.code);
          },
        },
        { field: "name", title: "标的名称", width: "48%" },
      ]],
      text: { none: "没有匹配的成分股" },
      page: false,
      size: "sm",
      skin: "row",
      even: true,
      height: TABLE_HEIGHT,
      done: function () {
        $("#bkgn_stock_total_tip").text("共 " + data.length + " 只成分股");
        bind_stock_table_keyboard();
        // 打开某板块后自动聚焦 wrapper, 让 ↑/↓ 立即可用(搜索过滤重渲染不抢焦点)。
        if (opts.focus) $("#bkgn_stock_wrap").focus();
      },
    });
    if (!stockRowClickBound) {
      layui.table.on("row(bkgn_stock_table)", on_stock_row_click);
      stockRowClickBound = true;
    }
  }

  function on_stock_row_click(obj) {
    currentStockIndex = obj.index;
    var data = obj.data;
    change_chart_ticker(Utils.get_market(), data.code);
    layui.table.setRowChecked("bkgn_stock_table", { index: "all", checked: false });
    layui.table.setRowChecked("bkgn_stock_table", { index: obj.index });
    // 让 wrapper 获得焦点，点完后可直接 ↑/↓ 继续浏览
    $("#bkgn_stock_wrap").focus();
  }

  // 键盘导航：在 layui table 渲染出的容器上监听 ↑/↓/Home/End/Enter
  // 切到目标行时立即调 change_chart_ticker，主图表跟着翻
  function bind_stock_table_keyboard() {
    // 键盘监听绑在稳定的外层 wrapper(#bkgn_stock_wrap)上, 而非 layui 每次渲染都重建的
    // .layui-table-view; keydown 从内层 view 冒泡到 wrapper, 内层重渲染不丢绑定。
    var $wrap = $("#bkgn_stock_wrap");
    if (!$wrap.length) return;
    $wrap.off("keydown.bkgn").on("keydown.bkgn", function (e) {
      var n = visibleStocks.length;
      if (!n) return;
      var key = e.key || "";
      if (key === "ArrowDown") {
        e.preventDefault();
        select_stock_index(currentStockIndex < 0 ? 0 : Math.min(currentStockIndex + 1, n - 1));
      } else if (key === "ArrowUp") {
        e.preventDefault();
        select_stock_index(currentStockIndex <= 0 ? 0 : currentStockIndex - 1);
      } else if (key === "Home") {
        e.preventDefault();
        select_stock_index(0);
      } else if (key === "End") {
        e.preventDefault();
        select_stock_index(n - 1);
      } else if (key === "Enter" && currentStockIndex >= 0) {
        e.preventDefault();
        var item = visibleStocks[currentStockIndex];
        if (item) change_chart_ticker(Utils.get_market(), item.code);
      }
    });
  }

  function select_stock_index(idx) {
    if (idx < 0 || idx >= visibleStocks.length) return;
    if (idx === currentStockIndex) return;
    currentStockIndex = idx;
    var item = visibleStocks[idx];
    layui.table.setRowChecked("bkgn_stock_table", { index: "all", checked: false });
    layui.table.setRowChecked("bkgn_stock_table", { index: idx });
    var $row = $('#bkgn_stock_wrap .layui-table-body tr[data-index="' + idx + '"]');
    if ($row.length && $row[0].scrollIntoView) {
      $row[0].scrollIntoView({ block: "nearest" });
    }
    change_chart_ticker(Utils.get_market(), item.code);
  }

  // 搜索框绑定：keyup 防抖 + Enter 即时触发、查询按钮、重置按钮三件套
  function bind_search_box(input, btn, reset, doSearch) {
    var timer = null;
    $(input).on("keyup", function (e) {
      var kw = $(this).val().trim();
      clearTimeout(timer);
      if (e.key === "Enter") {
        doSearch(kw);
      } else {
        timer = setTimeout(function () { doSearch(kw); }, SEARCH_DEBOUNCE_MS);
      }
    });
    $(btn).on("click", function () { doSearch($(input).val().trim()); });
    $(reset).on("click", function () { $(input).val(""); doSearch(""); });
  }

  function bind_search_handlers() {
    if (searchHandlersBound) return;
    bind_search_box("#bkgn_search", "#bkgn_search_btn", "#bkgn_reset_btn", do_bkgn_search);
    bind_search_box("#bkgn_stock_search", "#bkgn_stock_search_btn", "#bkgn_stock_reset_btn", do_stock_search);
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

  return { init_bkgn_opts: init_bkgn_opts };
})();
