var AI = (function () {
  var tableInstanceId = "table_ai_analysis";
  var isTableRendered = false;

  return {
    get_ai_analyse_records: function () {
      // 剥离 AI 回复中可能包裹的 markdown 代码块标记（支持只有开头无结尾的情况）
      function stripMarkdownCodeBlock(md) {
        const startMatch = md.match(/^```(?:markdown)?\s*/i);
        const endMatch = md.match(/\s*```\s*$/);
        if (startMatch && endMatch) {
          return md
            .replace(/^```(?:markdown)?\s*/i, "")
            .replace(/\s*```\s*$/, "");
        } else if (startMatch) {
          return md.replace(/^```(?:markdown)?\s*/i, "");
        }
        return md;
      }
      layui.use(["table"], function () {
        let table = layui.table;
        var element = layui.element;

        // 表格已渲染时只 reload 数据，避免重复创建实例
        if (isTableRendered) {
          table.reload(tableInstanceId, {
            url: "/ai/analyse_records/" + Utils.get_market(),
            page: {
              curr: 1,
            },
          });
          return;
        }

        table.render({
          elem: "#table_ai_analysis",
          id: tableInstanceId,
          defaultContextmenu: false,
          url: "/ai/analyse_records/" + Utils.get_market(),
          page: true,
          limit: 10,
          limits: [10, 20, 30, 50, 100],
          className: "layui-font-12",
          size: "sm",
          maxHeight: 750,
          cols: [
            [
              { field: "stock_name", title: "名称", sort: false, width: 100 },
              { field: "stock_code", title: "代码", sort: false, width: 80 },
              { field: "frequency", title: "周期", sort: false, width: 60 },
              { field: "dt", title: "时间", sort: false, width: 160 },
            ],
          ],
        });
        isTableRendered = true;
        table.on("row(table_ai_analysis)", function (obj) {
          let data = obj.data;
          var title =
            "AI分析 " +
            data.stock_code +
            " " +
            data.stock_name +
            " " +
            data.frequency +
            data.dt +
            " 模型 " +
            data.model;
          var show_html =
            '<div class="layui-collapse ai-analyse-div" lay-filter="collapse-ais"><div class="layui-colla-item"><div class="layui-colla-title">缠论状态提示词</div><div class="layui-colla-content">' +
            marked.parse(stripMarkdownCodeBlock(data.prompt)) +
            "</div></div>" +
            '<div class="layui-colla-item"><div class="layui-colla-title">AI分析结果</div><div class="layui-colla-content layui-show">' +
            marked.parse(stripMarkdownCodeBlock(data.msg)) +
            "</div></div></div>";
          layer.open({
            type: 1,
            title: title,
            content: show_html,
            area: ["720px", "650px"],
            anim: "slideLeft",
            shade: 0,
          });
          element.render("collapse", "collapse-ais");

          change_chart_ticker(Utils.get_market(), data.stock_code);
          $("#ai_code").val(data.stock_code);
        });
      });
    },
    init_ai_opts: function () {
      let ai_frequencys = $("#ai_frequencys");
      $(ai_frequencys).html();
      layui.each(market_frequencys[Utils.get_market()], function (i, f) {
        $(ai_frequencys).append("<option value='" + f + "'>" + f + "</option>");
      });
      layui.form.render($(ai_frequencys));
      $(ai_frequencys)
        .siblings("div.layui-form-select")
        .find("dl")
        .find("dd[lay-value=d]")
        .click();

      $("#ai_code").val(Utils.get_code());
      $("#ai_analyse_btn").click(function () {
        // 防重复点击：请求期间禁用按钮
        $("#ai_analyse_btn")
          .addClass("layui-btn-disabled")
          .attr("disabled", true);
        $("#ai_analyse_btn").html("分析中...");
        $.ajax({
          type: "POST",
          url: "/ai/analyse",
          data: {
            market: Utils.get_market(),
            code: $("#ai_code").val(),
            frequency: $("#ai_frequencys").val(),
          },
          dataType: "json",
          success: function (res) {
            if (res["ok"] === true) {
              layer.msg("分析成功");
              AI.get_ai_analyse_records();
            } else {
              layer.msg(res["msg"]);
            }
            $("#ai_analyse_btn")
              .removeClass("layui-btn-disabled")
              .attr("disabled", false);
            $("#ai_analyse_btn").html("分析");
          },
          error: function (res) {
            layer.msg("分析失败，查看控制台，查找错误问题");
            $("#ai_analyse_btn")
              .removeClass("layui-btn-disabled")
              .attr("disabled", false);
            $("#ai_analyse_btn").html("分析");
          },
        });
      });
    },
  };
})();
