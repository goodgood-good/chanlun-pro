var Utils = (function () {
  function is_embedded_chart() {
    if (window.__CHANLUN_EMBEDDED_CHART === true) return true;
    try {
      return new URLSearchParams(window.location.search || "")
        .get("chart_embed") === "decision-support";
    } catch (e) {
      return false;
    }
  }

  function embedded_settings() {
    if (!window.__chanlunEmbeddedChartSettings) {
      window.__chanlunEmbeddedChartSettings = Object.create(null);
    }
    return window.__chanlunEmbeddedChartSettings;
  }

  function account_tv_chart_value(key) {
    if (!window.AccountPreferences || typeof window.AccountPreferences.getItem !== "function") {
      return undefined;
    }
    try {
      var raw = window.AccountPreferences.getItem("tv_chart");
      var parsed = raw ? JSON.parse(raw) : null;
      return parsed && Object.prototype.hasOwnProperty.call(parsed, key)
        ? parsed[key]
        : undefined;
    } catch (e) {
      return undefined;
    }
  }

  function get_selected_items() {
    try {
      let raw = localStorage.getItem(Utils.get_market() + "_selectedItems");
      if (!raw) return [];
      let parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(function (item) {
        return item && typeof item.name === "string" &&
          typeof item.value === "string" && item.value.length > 0;
      });
    } catch (e) {
      return [];
    }
  }

  return {
    get_local_data: function (key) {
      if (is_embedded_chart()) {
        var overlay = embedded_settings();
        if (Object.prototype.hasOwnProperty.call(overlay, key)) return overlay[key];
        var accountValue = account_tv_chart_value(key);
        if (accountValue !== undefined) return accountValue;
      }
      if (layui.data("tv_chart")) {
        let val = layui.data("tv_chart")[key];
        if (val === undefined) {
          return default_vals[key];
        }
        return val;
      }
      return default_vals[key];
    },
    set_local_data: function (key, val) {
      if (is_embedded_chart()) {
        embedded_settings()[key] = val;
        return;
      }
      layui.data("tv_chart", {
        key: key,
        value: val,
      });
      if (window.AccountPreferences && typeof window.AccountPreferences.captureTvChart === "function") {
        window.AccountPreferences.captureTvChart();
      }
    },
    get_selected_items: get_selected_items,
    add_to_cache: function (data) {
      let selectedItems = get_selected_items();

      selectedItems.unshift({
        name: data.arr[0].name,
        value: data.arr[0].value,
      });

      // 最多保留30条，先截断再去重，保留最近搜索顺序
      selectedItems = selectedItems.slice(0, 30);

      const uniqueItems = [];
      const seenValues = new Set();

      for (const item of selectedItems) {
        if (!seenValues.has(item.value)) {
          seenValues.add(item.value);
          uniqueItems.push(item);
        }
      }

      localStorage.setItem(
        Utils.get_market() + "_selectedItems",
        JSON.stringify(uniqueItems)
      );
    },
    get_market: function () {
      return Utils.get_local_data("market");
    },
    get_code: function () {
      return Utils.get_local_data(Utils.get_market() + "_code");
    },
    // 渲染底部固定工具栏，提供侧边菜单栏的折叠/展开按钮
    render_fixbar: function () {
      layui.use(function () {
        var util = layui.util;
        util.fixbar({
          bars: [
            {
              type: "hide_menu",
              icon: "layui-icon-spread-left",
            },
          ],
          default: false,
          on: {},
          click: function (type) {
            if (type === "hide_menu") {
              var fixed_li = $(".layui-fixbar  li:first-child");
              if (fixed_li.attr("class").includes("layui-icon-spread-left")) {
                fixed_li
                  .removeClass("layui-icon-spread-left")
                  .addClass("layui-icon-shrink-right");
                $("#chart_menu").hide();
                $("#chart_container")
                  .removeClass("layui-col-xs10 layui-col-sm10 layui-col-md10")
                  .addClass("layui-col-xs12 layui-col-sm12 layui-col-md12");
              } else {
                fixed_li
                  .removeClass("layui-icon-shrink-right")
                  .addClass("layui-icon-spread-left");
                $("#chart_menu").show();
                $("#chart_container")
                  .removeClass("layui-col-xs12 layui-col-sm12 layui-col-md12")
                  .addClass("layui-col-xs10 layui-col-sm10 layui-col-md10");
              }
            }
          },
        });
      });
    },
  };
})();
