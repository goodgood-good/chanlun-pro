var Alert = (function () {
  function textTemplate(value) {
    var span = document.createElement("span");
    span.textContent = String(value == null ? "" : value);
    return span.outerHTML;
  }

  return {
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
              { field: "task_name", title: "任务名称", templet: function (d) { return textTemplate(d.task_name); } },
              {
                field: "zx_group",
                title: "自选分组",
                templet: function (d) {
                  return textTemplate(d.zx_group);
                },
              },
              {
                field: "frequency",
                title: "检测周期",
                templet: function (d) {
                  return textTemplate(d.frequency);
                },
              },
              {
                field: "interval_minutes",
                title: "执行间隔（分钟）",
                sort: true,
                templet: function (d) {
                  return textTemplate(d.interval_minutes);
                },
              },
              {
                field: "check_bi_type",
                title: "笔方向条件",
                templet: function (d) {
                  return textTemplate(d.check_bi_type);
                },
              },
              {
                field: "check_bi_beichi",
                title: "笔背驰条件",
                templet: function (d) {
                  return textTemplate(d.check_bi_beichi);
                },
              },
              {
                field: "check_bi_mmd",
                title: "笔买卖点条件",
                templet: function (d) {
                  return textTemplate(d.check_bi_mmd);
                },
              },
              {
                field: "check_xd_type",
                title: "线段方向条件",
                templet: function (d) {
                  return textTemplate(d.check_xd_type);
                },
              },
              {
                field: "check_xd_beichi",
                title: "线段背驰条件",
                templet: function (d) {
                  return textTemplate(d.check_xd_beichi);
                },
              },
              {
                field: "check_xd_mmd",
                title: "线段买卖点条件",
                templet: function (d) {
                  return textTemplate(d.check_xd_mmd);
                },
              },
              {
                field: "is_send_msg",
                title: "消息通知",
                sort: true,
                templet: function (d) {
                  if (d.is_send_msg === 1) {
                    return "发送";
                  } else {
                    return "不发送";
                  }
                },
              },
              {
                field: "is_run",
                title: "运行状态",
                sort: true,
                templet: function (d) {
                  if (d.is_run === 1) {
                    return "运行中";
                  } else {
                    return "已停用";
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
            title: "编辑预警任务",
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
            data: [{ title: "删除任务", id: "del" }],
            click: function (menuData, othis) {
              if (menuData["id"] === "del") {
                AppRequest.ajax({
                  type: "POST",
                  url: "/alert_del/" + data.id,
                  dataType: "json",
                  success: function (res) {
                    if (res["ok"]) {
                      layer.msg("预警任务已删除");
                    } else {
                      layer.msg("预警任务删除失败");
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
