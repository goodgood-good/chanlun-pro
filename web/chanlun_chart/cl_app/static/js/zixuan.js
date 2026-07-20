var ZiXuan = (function () {
  var zx_group = "我的关注";
  var timeout_update_rates = null;
  var update_request_in_flight = false;
  var update_retry_index = 0;
  var update_poll_generation = 0;
  var rate_polling_active = true;
  var UPDATE_NORMAL_DELAY_MS = 3000;
  var UPDATE_CLOSED_DELAY_MS = 300000;
  var UPDATE_RETRY_DELAYS_MS = [6000, 12000, 24000, 30000];
  var zixuanOptsRequestGeneration = 0;
  var stockListRequestGeneration = 0;
  var groupsRequestGeneration = 0;
  var searchRequestGeneration = 0;
  var stockTableHandlersBound = false;
  var groupUiBound = false;
  var createGroupRequestInFlight = false;

  function appAjax(options) {
    var requestOptions = Object.assign({ timeout: 10000 }, options || {});
    if (typeof requestOptions.error !== "function") {
      requestOptions.error = function () {
        if (window.layer) layer.msg("数据请求失败，请稍后重试");
      };
    }
    if (window.AppRequest && typeof window.AppRequest.ajax === "function") {
      return window.AppRequest.ajax(requestOptions);
    }
    return $.ajax(requestOptions);
  }

  function pathSegment(value) {
    return encodeURIComponent(String(value == null ? "" : value));
  }

  // Stop the active polling generation before a group or table is replaced.
  function stop_timer() {
    if (timeout_update_rates !== null) {
      clearTimeout(timeout_update_rates);
      timeout_update_rates = null;
    }
    update_poll_generation += 1;
    update_retry_index = 0;
  }

  function schedule_rate_update(delay, generation) {
    if (!rate_polling_active) return;
    if (generation !== update_poll_generation) return;
    if (timeout_update_rates !== null) clearTimeout(timeout_update_rates);
    timeout_update_rates = setTimeout(function () {
      timeout_update_rates = null;
      ZiXuan.stocks_update_rate(generation);
    }, delay);
  }

  function schedule_rate_retry(generation) {
    var delay = UPDATE_RETRY_DELAYS_MS[
      Math.min(update_retry_index, UPDATE_RETRY_DELAYS_MS.length - 1)
    ];
    update_retry_index = Math.min(
      update_retry_index + 1,
      UPDATE_RETRY_DELAYS_MS.length - 1
    );
    schedule_rate_update(delay, generation);
  }

  function checkboxTemplate(name, checked) {
    var span = document.createElement("span");
    var input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked === true;
    input.defaultChecked = checked === true;
    span.appendChild(input);
    span.appendChild(document.createTextNode(" " + String(name || "")));
    return span.outerHTML;
  }

  function rateNode(code, price, rate, color) {
    var root = document.createElement("div");
    root.className = "code_rate";
    root.dataset.code = String(code || "");
    if (color) root.style.color = color;
    var rateLine = document.createElement("div");
    rateLine.className = "layui-font-14";
    if (color) rateLine.style.color = color;
    rateLine.textContent = rate == null ? "- %" : String(rate) + "%";
    var priceLine = document.createElement("div");
    priceLine.className = "layui-font-12";
    priceLine.textContent = price == null ? "-" : String(price);
    root.appendChild(rateLine);
    root.appendChild(priceLine);
    return root;
  }

  function stockNode(name, code, color) {
    var root = document.createElement("div");
    var nameLine = document.createElement("div");
    nameLine.className = "layui-font-14";
    if (color) nameLine.style.color = color;
    nameLine.textContent = String(name || "");
    var codeLine = document.createElement("div");
    codeLine.className = "layui-font-12 layui-font-gray";
    codeLine.textContent = String(code || "");
    root.appendChild(nameLine);
    root.appendChild(codeLine);
    return root;
  }

  function validateGroupName(value) {
    if (typeof value !== "string") {
      return { ok: false, name: "", message: "分组名称不能为空" };
    }
    var name = value.trim();
    if (!name) {
      return { ok: false, name: "", message: "分组名称不能为空" };
    }
    if (name.length > 64) {
      return { ok: false, name: name, message: "分组名称不能超过 64 个字符" };
    }
    if (/[\/\\\u0000-\u001f]/.test(name)) {
      return { ok: false, name: name, message: "分组名称不能包含斜杠或控制字符" };
    }
    return { ok: true, name: name, message: "" };
  }

  function setWatchStatus(message, state) {
    var status = $("#zixuan_watch_status");
    status.text(String(message || ""));
    status.attr("data-state", state || "idle");
  }

  function setCurrentGroupLabel(group) {
    $("#zixuan_current_group").text(group || "未选择分组");
  }

  function setGroupError(message) {
    var error = $("#zixuan_group_error");
    error.text(String(message || ""));
    error.prop("hidden", !message);
  }

  function setCreateGroupBusy(busy) {
    var submit = $("#zixuan_group_submit");
    submit.prop("disabled", busy === true);
    submit.toggleClass("is-busy", busy === true);
    submit.text(busy === true ? "创建中…" : "创建分组");
  }

  function setGroupCreatorOpen(open) {
    var isOpen = open === true;
    var creator = $("#zixuan_group_creator");
    creator.prop("hidden", !isOpen);
    creator.attr("aria-hidden", String(!isOpen));
    $("#create_zixuan_group").attr("aria-expanded", String(isOpen));
    if (isOpen) {
      setGroupError("");
      $("#zixuan_group_name").focus();
    }
  }

  function responseMessage(xhr, fallback) {
    if (xhr && xhr.responseJSON && xhr.responseJSON.msg) {
      return String(xhr.responseJSON.msg);
    }
    return fallback;
  }

  function bindGroupControls(layer) {
    if (groupUiBound) return;
    if (!document.getElementById || !document.getElementById("zixuan_watch_panel")) {
      return;
    }
    groupUiBound = true;

    $("#create_zixuan_group")
      .off("click.zixuanGroup")
      .on("click.zixuanGroup", function () {
        var open = $(this).attr("aria-expanded") !== "true";
        setGroupCreatorOpen(open);
      });
    $("#zixuan_group_cancel")
      .off("click.zixuanGroup")
      .on("click.zixuanGroup", function () {
        setGroupCreatorOpen(false);
      });
    $("#zixuan_group_creator")
      .off("submit.zixuanGroup")
      .on("submit.zixuanGroup", function (event) {
        event.preventDefault();
        ZiXuan.create_group($("#zixuan_group_name").val());
      });
    $("#manage_zixuan_groups")
      .off("click.zixuanGroup")
      .on("click.zixuanGroup", function () {
        var viewportWidth = Number(window.innerWidth) || 960;
        var modalWidth = Math.max(320, Math.min(760, viewportWidth - 32));
        layer.open({
          type: 2,
          title: "管理自选分组",
          area: [modalWidth + "px", "82vh"],
          content: "/zixuan_group/" + pathSegment(Utils.get_market()),
          fixed: true,
          shadeClose: true,
          end: function () {
            ZiXuan.load_groups(ZiXuan.zx_group);
            ZiXuan.render_zixuan_opts();
          },
        });
      });
  }

  return {
    zx_group: zx_group,
    set_group_creator_open: setGroupCreatorOpen,
    create_group: function (rawName) {
      if (createGroupRequestInFlight) return false;
      var validation = validateGroupName(
        typeof rawName === "string" ? rawName : $("#zixuan_group_name").val()
      );
      if (!validation.ok) {
        setGroupError(validation.message);
        return false;
      }

      var market = Utils.get_market();
      setGroupError("");
      setCreateGroupBusy(true);
      createGroupRequestInFlight = true;
      appAjax({
        type: "POST",
        url: "/opt_zixuan_group/" + pathSegment(market),
        data: { opt: "ADD", zx_group: validation.name },
        dataType: "json",
        timeout: 10000,
        success: function (response) {
          if (!response || response.ok !== true) {
            var failedMessage = response && response.msg
              ? String(response.msg)
              : "分组创建失败";
            setGroupError(failedMessage);
            if (window.layer) layer.msg(failedMessage);
            return;
          }
          var createdGroup = String(response.group || validation.name);
          $("#zixuan_group_name").val("");
          setGroupCreatorOpen(false);
          if (window.layer) layer.msg(response.msg || "自选分组已创建");
          ZiXuan.load_groups(createdGroup, function () {
            ZiXuan.render_zixuan_opts();
          });
        },
        error: function (xhr) {
          var message = responseMessage(xhr, "分组创建失败，请稍后重试");
          setGroupError(message);
          if (window.layer) layer.msg(message);
        },
        complete: function () {
          createGroupRequestInFlight = false;
          setCreateGroupBusy(false);
        },
      });
      return true;
    },
    load_groups: function (preferredGroup, onLoaded) {
      var market = Utils.get_market();
      var requested = validateGroupName(preferredGroup || "");
      var preferred = requested.ok ? requested.name : "";
      var generation = ++groupsRequestGeneration;
      setWatchStatus("正在读取自选分组…", "loading");

      appAjax({
        type: "GET",
        url: "/get_zixuan_groups/" + pathSegment(market),
        dataType: "json",
        timeout: 10000,
        success: function (response) {
          if (generation !== groupsRequestGeneration || market !== Utils.get_market()) {
            return;
          }
          var groups = (Array.isArray(response) ? response : []).filter(function (item) {
            return item && validateGroupName(item.name).ok;
          });
          var names = groups.map(function (item) { return String(item.name).trim(); });
          var selected = "";
          if (preferred && names.indexOf(preferred) !== -1) selected = preferred;
          else if (names.indexOf(ZiXuan.zx_group) !== -1) selected = ZiXuan.zx_group;
          else if (names.length > 0) selected = names[0];

          var groupSelect = $("#zixuan_groups");
          groupSelect.empty();
          layui.each(groups, function (_index, item) {
            groupSelect.append($("<option>", {
              value: String(item.name).trim(),
              text: String(item.name).trim(),
            }));
          });
          var canSelectValue = typeof groupSelect.val === "function";
          if (canSelectValue && selected) groupSelect.val(selected);
          layui.form.render("select");

          $("#zixuan_group_count").text(String(groups.length));
          if (!selected) {
            $("#zixuan_stock_count").text("0");
            setCurrentGroupLabel("未创建分组");
            setWatchStatus("尚未创建自选分组", "empty");
          } else {
            ZiXuan.zx_group = selected;
            setCurrentGroupLabel(selected);
            if (canSelectValue) ZiXuan.render_zixuan_stocks();
          }
          if (typeof onLoaded === "function") onLoaded(groups, selected);
        },
        error: function () {
          if (generation === groupsRequestGeneration) {
            setWatchStatus("自选分组加载失败", "error");
            if (window.layer) layer.msg("自选分组加载失败，请稍后重试");
          }
        },
      });
    },
    render_zixuan_opts: function () {
      var market = Utils.get_market();
      var code = String(Utils.get_code() || "").replace(/\//g, "__");
      var generation = ++zixuanOptsRequestGeneration;
      appAjax({
        type: "GET",
        url: "/get_stock_zixuan/" + pathSegment(market) + "/" + pathSegment(code),
        dataType: "json",
        timeout: 10000,
        success: function (res) {
          if (generation !== zixuanOptsRequestGeneration
              || market !== Utils.get_market()
              || code !== String(Utils.get_code() || "").replace(/\//g, "__")) return;
          let data = [];
          layui.each(Array.isArray(res) ? res : [], function (i, e) {
            let templet = checkboxTemplate(e["zx_name"], e["exists"] !== 0);
            data.push({
              title: e["zx_name"],
              id: i,
              templet: templet,
              exists: e["exists"],
              code: e["code"],
            });
          });

          $("#zixuan_groups").change();
          layui.dropdown.reloadData("add_zixuan", { data: data });
        },
        error: function () {
          if (generation === zixuanOptsRequestGeneration && window.layer) {
            layer.msg("自选分组状态加载失败");
          }
        },
      });
    },
    set_rate_polling_active: function (is_active) {
      var next_active = is_active === true;
      if (next_active === rate_polling_active) return;

      rate_polling_active = next_active;
      if (!next_active) {
        stop_timer();
        return;
      }

      update_retry_index = 0;
      ZiXuan.stocks_update_rate(update_poll_generation);
    },

    // 批量请求当前列表中所有股票的实时涨跌幅并刷新 DOM
    stocks_update_rate: function (generation) {
      if (!rate_polling_active) return false;
      var request_generation =
        typeof generation === "number" ? generation : update_poll_generation;
      if (request_generation !== update_poll_generation) return false;
      if (update_request_in_flight) return false;

      let codes = [];
      $(".code_rate").each(function () {
        codes.push($(this).data("code"));
      });
      if (codes.length === 0) {
        setWatchStatus("当前分组暂无标的", "empty");
        return true;
      }

      update_request_in_flight = true;
      var completion_state = "retry";
      appAjax({
        type: "POST",
        url: "/ticks",
        data: { market: Utils.get_market(), codes: JSON.stringify(codes) },
        dataType: "json",
        timeout: 8000,
        success: function (response) {
          if (request_generation !== update_poll_generation) return;
          if (
            !response ||
            response.ok !== true ||
            (response.market_state !== "open" &&
              response.market_state !== "closed" &&
              response.market_state !== "unknown") ||
            !Array.isArray(response.ticks)
          ) {
            return;
          }

          for (let i = 0; i < response.ticks.length; i++) {
            let tick = response.ticks[i];
            if (!tick || typeof tick !== "object") continue;
            let rate = Number(tick.rate);
            let price = Number(tick.price);
            if (!Number.isFinite(rate)) continue;
            let color = "#1e9fff"; // flat
            if (rate > 0) color = "#ff5722";
            else if (rate < 0) color = "#16baaa";

            let obj_span_rate = $(".code_rate").filter(function () {
              return String($(this).data("code")) === String(tick.code);
            });
            var next = rateNode(
              tick.code,
              Number.isFinite(price) ? price : null,
              rate,
              color
            );
            obj_span_rate.replaceWith(next);
          }
          completion_state = response.market_state;
          setWatchStatus(
            response.market_state === "closed" ? "休市 · 低频检查" : "实时行情更新中",
            response.market_state === "closed" ? "closed" : "live"
          );
        },
        error: function () {
          completion_state = "retry";
          setWatchStatus("行情连接中断，准备重试", "error");
        },
        complete: function () {
          update_request_in_flight = false;

          if (request_generation !== update_poll_generation) {
            if (rate_polling_active) {
              schedule_rate_update(0, update_poll_generation);
            }
            return;
          }
          if (completion_state === "closed") {
            update_retry_index = 0;
            schedule_rate_update(UPDATE_CLOSED_DELAY_MS, request_generation);
            return;
          }
          if (completion_state === "open" || completion_state === "unknown") {
            update_retry_index = 0;
            schedule_rate_update(UPDATE_NORMAL_DELAY_MS, request_generation);
            return;
          }
          schedule_rate_retry(request_generation);
        },
      });
    },

    render_zixuan_stocks: function () {
      stop_timer();
      $("#zixuan_stock_count").text("—");
      setCurrentGroupLabel(ZiXuan.zx_group);
      setWatchStatus("正在加载分组标的…", "loading");

      layui.use(["table", "dropdown", "util"], function () {
        let table = layui.table;
        let dropdown = layui.dropdown;

        var market = Utils.get_market();
        var group = ZiXuan.zx_group;
        var requestGeneration = ++stockListRequestGeneration;
        appAjax({
          type: "GET",
          url: "/get_zixuan_stocks/" + pathSegment(market) + "/" + pathSegment(group),
          dataType: "json",
          timeout: 10000,
          success: function (response) {
            if (requestGeneration !== stockListRequestGeneration
                || market !== Utils.get_market()
                || group !== ZiXuan.zx_group) return;
            var rows = Array.isArray(response)
              ? response
              : (response && Array.isArray(response.data) ? response.data : []);
            $("#zixuan_stock_count").text(String(rows.length));
            setCurrentGroupLabel(group);
            setWatchStatus(
              rows.length > 0 ? "标的已加载，连接实时行情…" : "当前分组暂无标的",
              rows.length > 0 ? "loading" : "empty"
            );
            table.render({
              elem: "#table_zixuan_list",
              defaultContextmenu: false,
              data: rows,
              page: false,
              className: "layui-font-12",
              size: "sm",
              lineStyle: "height: 52px;",
              loading: false,
              cols: [[
                {
                  field: "code",
                  title: "关注标的",
                  sort: false,
                  templet: function (d) {
                    return stockNode(d.name, d.code, d.color).outerHTML;
                  },
                },
                {
                  field: "zf",
                  title: "涨跌 / 现价",
                  sort: false,
                  width: 70,
                  templet: function (d) {
                    return rateNode(d.code, null, null, null).outerHTML;
                  },
                },
              ]],
              text: { none: "当前分组暂无标的" },
              done: function () {
                if (requestGeneration === stockListRequestGeneration) {
                  ZiXuan.stocks_update_rate();
                }
              },
            });
          },
          error: function () {
            if (requestGeneration === stockListRequestGeneration && window.layer) {
              setWatchStatus("自选标的加载失败", "error");
              layer.msg("自选标的加载失败，请稍后重试");
            }
          },
        });
        if (!stockTableHandlersBound) {
          stockTableHandlersBound = true;
        table.on("row(table_zixuan_list)", function (obj) {
          const data = obj.data;
          const code = data.code;
          change_chart_ticker(Utils.get_market(), code);
          table.setRowChecked("table_zixuan_list", {
            index: "all",
            checked: false,
          });
          table.setRowChecked("table_zixuan_list", {
            index: obj.index,
          });
        });

        table.on("rowContextmenu(table_zixuan_list)", function (obj) {
          let data = obj.data;
          let menu_data = [
            { title: "从当前分组移除", id: "del" },
            { title: "移至顶部", id: "sort_1", direction: "top" },
            { title: "移至底部", id: "sort_2", direction: "bottom" },
            {
                title: "标记颜色",
                id: "color_1",
                color: "#ff5722",
                templet: function () { return '<div class="layui-bg-red">红色标记</div>'; },
            },
            {
                title: "标记颜色",
                id: "color_2",
                color: "#ffb800",
                templet: function () { return '<div class="layui-bg-orange">橙色标记</div>'; },
            },
            {
                title: "标记颜色",
                id: "color_3",
                color: "#16baaa",
                templet: function () { return '<div class="layui-bg-green">绿色标记</div>'; },
            },
            {
                title: "标记颜色",
                id: "color_4",
                color: "#1e9fff",
                templet: function () { return '<div class="layui-bg-blue">蓝色标记</div>'; },
            },
            {
                title: "标记颜色",
                id: "color_5",
                color: "#a233c6",
                templet: function () { return '<div class="layui-bg-purple">紫色标记</div>'; },
            },
            {
                title: "标记颜色",
                id: "color_6",
                color: "",
                templet: function () { return '<div class="layui-bg-gray">清除标记</div>'; },
            },
          ];

          if (Utils.get_market() === "a") {
            menu_data.splice(3, 0, { title: "查看公司资料", id: "dfcf" });
          }

          dropdown.render({
            trigger: "contextmenu",
            show: true,
            data: menu_data,
            click: function (menuData, othis) {
              if (menuData["id"] === "del") {
                appAjax({
                  type: "POST",
                  url: "/set_stock_zixuan",
                  data: {
                    opt: "DEL",
                    market: Utils.get_market(),
                    group_name: ZiXuan.zx_group,
                    code: data.code,
                    color: "",
                    direction: "",
                  },
                  dataType: "json",
                  success: function (res) {
                    if (res["ok"]) {
                      layer.msg("已从当前自选分组移除");
                      obj.del();
                    } else {
                      layer.msg("移出自选分组失败");
                    }
                  },
                });
              } else if (String(menuData["id"] || "").indexOf("color_") === 0) {
                 appAjax({
                    type: "POST",
                    url: "/set_stock_zixuan",
                    data: {
                      opt: "COLOR",
                      market: Utils.get_market(),
                      group_name: ZiXuan.zx_group,
                      code: data.code,
                      color: menuData["color"],
                      direction: "",
                    },
                    dataType: "json",
                    success: function (res) {
                      if (res && res.ok) {
                        obj.update({ color: menuData["color"] }, true);
                      } else {
                        layer.msg("标记颜色更新失败");
                      }
                    },
                  });
              } else if (
                menuData["id"] === "sort_1" ||
                menuData["id"] === "sort_2"
              ) {
                appAjax({
                    type: "POST",
                    url: "/set_stock_zixuan",
                    data: {
                      opt: "SORT",
                      market: Utils.get_market(),
                      group_name: ZiXuan.zx_group,
                      code: data.code,
                      color: "",
                      direction: menuData["direction"],
                    },
                    dataType: "json",
                    success: function (res) {
                      if (res && res.ok) {
                        ZiXuan.render_zixuan_stocks();
                      } else {
                        layer.msg("自选排序更新失败");
                      }
                    },
                  });
              } else if (menuData["id"] === "dfcf") {
                window.open(
                  "https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code=" +
                    encodeURIComponent(data.code.replace(".", ""))
                );
              }
            },
          });
        });
        }
      });
    },

    init_zixuan_opts: function () {
        layui.use(function () {
           var layer = layui.layer;
           var dropdown = layui.dropdown;
           var form = layui.form;
           bindGroupControls(layer);
           ZiXuan.load_groups(ZiXuan.zx_group);
            dropdown.render({
                elem: "#add_zixuan",
                data: [],
                click: function (data, othis) {
                    let opt = "ADD";
                    if (data["exists"] === 1) {
                        opt = "DEL";
                    }
                    appAjax({
                        type: "POST",
                        url: "/set_stock_zixuan",
                        data: {
                            opt: opt,
                            market: Utils.get_market(),
                            group_name: data["title"],
                            code: data["code"],
                            color: "",
                            direction: "",
                        },
                        dataType: "json",
                        success: function (res) {
                            if (!res || !res.ok) {
                                layer.msg("自选分组更新失败");
                                return;
                            }
                            if (data["title"] == ZiXuan.zx_group) {
                                ZiXuan.render_zixuan_opts();
                                ZiXuan.render_zixuan_stocks();
                            }
                        },
                    });
                    return false;
                },
            });

            form.on("select(select_zx_group)", function (data) {
                ZiXuan.zx_group = data.value;
                setCurrentGroupLabel(data.value);
                ZiXuan.render_zixuan_stocks();
            });

            $("#refresh_zixuan").click(function () {
                setWatchStatus("正在手动刷新…", "loading");
                ZiXuan.render_zixuan_stocks();
            });

             const searchSelect = xmSelect.render({
                el: "#code_search",
                filterable: true,
                remoteSearch: true,
                radio: true,
                clickClose: true,
                tips: "输入代码、名称或拼音",
                empty: "未找到匹配标的",
                theme: { color: "#e54d42" },
                delay: 1000,
                remoteMethod: function (val, cb, show) {
                    var requestGeneration = ++searchRequestGeneration;
                    if (val) {
                        var searchMarket = Utils.get_market();
                        appAjax({
                            type: "GET",
                            url: "/tv/search",
                            data: {
                                limit: 30,
                                type: "",
                                query: val,
                                exchange: searchMarket,
                            },
                            dataType: "json",
                            timeout: 10000,
                            success: function (res) {
                                if (requestGeneration !== searchRequestGeneration
                                    || searchMarket !== Utils.get_market()) return;
                                let lst = [];
                                layui.each(Array.isArray(res) ? res : [], function (i, r) {
                                    lst.push({
                                        name: r["symbol"] + ":" + r["description"],
                                        value: r["symbol"],
                                    });
                                });
                                cb(lst);
                            },
                            error: function () {
                                if (requestGeneration === searchRequestGeneration) cb([]);
                            },
                        });
                    } else {
                        let storedItems = Utils.get_selected_items();
                        cb(storedItems);
                    }
                },                    show: function () {
                        let storedItems = Utils.get_selected_items();
                        searchSelect.update({ data: storedItems });
                },
                on: function (data) {
                    if (data.arr.length > 0) {
                        change_chart_ticker(Utils.get_market(), data.arr[0]["value"]);
                        Utils.add_to_cache(data);
                    }
                },
                data: [],
            });
        });
    }
  };
})();
