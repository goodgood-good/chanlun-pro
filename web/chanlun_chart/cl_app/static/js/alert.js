var Alert = (function () {
  function textTemplate(value) {
    var span = document.createElement("span");
    span.textContent = String(value == null ? "" : value);
    return span.outerHTML;
  }

  function alertRecordTemplate(d) {
    var row = document.createElement("div");
    row.className = "alert-record-row";
    var heading = document.createElement("div");
    heading.style.cssText = "font-weight:bold;font-size:14px";
    heading.appendChild(document.createTextNode(String(d.name || "") + " "));
    [[d.code, "#888"], [d.frequency, "#16baaa"], [d.line_type, "#b37feb"]]
      .forEach(function (item) {
        var span = document.createElement("span");
        span.style.color = item[1];
        span.textContent = String(item[0] || "");
        heading.appendChild(span);
        heading.appendChild(document.createTextNode(" "));
      });
    var message = document.createElement("div");
    message.style.fontSize = "16px";
    message.textContent = String(d.msg || "");
    var footer = document.createElement("div");
    footer.style.cssText = "color:#888;font-size:12px";
    footer.appendChild(document.createTextNode(String(d.datetime_str || "")));
    var task = document.createElement("span");
    task.style.cssText = "margin-left:10px;color:rgb(203,243,183)";
    task.textContent = String(d.task_name || "");
    footer.appendChild(task);
    row.appendChild(heading);
    row.appendChild(message);
    row.appendChild(footer);
    return row.outerHTML;
  }

  return {
    init: function () {
      layui.use(["table", "form"], function () {
        let form = layui.form;

        $.get("/alert_list/" + Utils.get_market(), function (res) {
          if (res.code == 0) {
            let task_name_select = $("#task_name_select");
            task_name_select.empty();
            task_name_select.append("<option value=''>全部</option>");
            $.each(res.data, function (index, item) {
              task_name_select.append(
                $("<option>", { value: item.task_name, text: item.task_name })
              );
            });
            form.render("select");
          }
        });

        form.on("select(task_name_select)", function (data) {
          Alert.get_alert_records();
        });
      });
    },

    get_alert_records: function () {
      layui.use(["table", "form"], function () {
        let table = layui.table;

        table.render({
          elem: "#table_alert_reocrds",
          defaultContextmenu: false,
          url:
            "/alert_records/" +
            Utils.get_market() +
            "?task_name=" +
            encodeURIComponent($("#task_name_select").val() || ""),
          page: false,
          className: "layui-font-12",
          size: "sm",
          maxHeight: 550,
          lineStyle: "height: auto;",
          cols: [
            [
              {
                field: "custom",
                title: "",
                templet: function (d) {
                  return alertRecordTemplate(d);
                },
              },
            ],
          ],
        });
        table.on("row(table_alert_reocrds)", function (obj) {
          let data = obj.data;
          change_chart_ticker(Utils.get_market(), data.code);
        });
      });
    },

    refresh_alerts_table: function () {
      layui.use(["table", "dropdown", "util"], function () {
        let table = layui.table;
        let dropdown = layui.dropdown;
        table.render({
          elem: "#table_alerts",
          defaultContextmenu: false,
          url: "/alert_list/" + Utils.get_market(),
          page: false,
          className: "layui-font-12",
          size: "sm",
          cols: [
            [
              { field: "task_name", title: "监控名称", templet: function (d) { return textTemplate(d.task_name); } },
              {
                field: "zx_group",
                title: "自选组",
                templet: function (d) {
                  return textTemplate(d.zx_group);
                },
              },
              {
                field: "frequency",
                title: "周期",
                templet: function (d) {
                  return textTemplate(d.frequency);
                },
              },
              {
                field: "interval_minutes",
                title: "运行间隔(分钟)",
                sort: true,
                templet: function (d) {
                  return textTemplate(d.interval_minutes);
                },
              },
              {
                field: "check_bi_type",
                title: "笔方向",
                templet: function (d) {
                  return textTemplate(d.check_bi_type);
                },
              },
              {
                field: "check_bi_beichi",
                title: "笔背驰",
                templet: function (d) {
                  return textTemplate(d.check_bi_beichi);
                },
              },
              {
                field: "check_bi_mmd",
                title: "笔买卖点",
                templet: function (d) {
                  return textTemplate(d.check_bi_mmd);
                },
              },
              {
                field: "check_xd_type",
                title: "线段方向",
                templet: function (d) {
                  return textTemplate(d.check_xd_type);
                },
              },
              {
                field: "check_xd_beichi",
                title: "线段背驰",
                templet: function (d) {
                  return textTemplate(d.check_xd_beichi);
                },
              },
              {
                field: "check_xd_mmd",
                title: "线段买卖点",
                templet: function (d) {
                  return textTemplate(d.check_xd_mmd);
                },
              },
              {
                field: "is_send_msg",
                title: "发送消息",
                sort: true,
                templet: function (d) {
                  if (d.is_send_msg === 1) {
                    return "发送";
                  } else {
                    return "不发";
                  }
                },
              },
              {
                field: "is_run",
                title: "启用",
                sort: true,
                templet: function (d) {
                  if (d.is_run === 1) {
                    return "启用";
                  } else {
                    return "禁用";
                  }
                },
              },
            ],
          ],
        });
        table.on("row(table_alerts)", function (obj) {
          let data = obj.data;
          layer.open({
            type: 2,
            title: "修改警报提醒",
            area: ["1000px", "90vh"],
            content: "/alert_edit/" + Utils.get_market() + "/" + data.id,
            anim: 1,
            fixed: true,
            shadeClose: true,
          });
        });
        table.on("rowContextmenu(table_alerts)", function (obj) {
          let data = obj.data;
          dropdown.render({
            trigger: "contextmenu",
            show: true,
            data: [{ title: "删除", id: "del" }],
            click: function (menuData, othis) {
              if (menuData["id"] === "del") {
                AppRequest.ajax({
                  type: "POST",
                  url: "/alert_del/" + data.id,
                  dataType: "json",
                  success: function (res) {
                    if (res["ok"]) {
                      layer.msg("删除成功");
                    } else {
                      layer.msg("删除失败");
                    }
                    Alert.refresh_alerts_table();
                  },
                });
              }
            },
          });
        });
      });
    },
  };
})();
