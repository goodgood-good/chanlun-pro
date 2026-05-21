# -*- coding: utf-8 -*-
"""
strength_compare.js 力度对比面板的 Playwright 前端验证（手动运行，非 pytest 用例）。

前置：``pip install playwright`` 后执行 ``python -m playwright install chromium``。
运行：仓库根目录下 ``.venv/Scripts/python.exe tests/signal_monitor/frontend/verify_panel.py``

做法：在临时目录生成「桩化测试页」（stub 掉 ``Utils`` / ``tvWidget`` / ``fetch``）
并拷一份 ``strength_compare.js``，用 headless chromium 跑一遍面板交互，校验
DOM 构建、上下文自动探测、加载→对比→渲染流程、零 JS 报错。

真实 web 应用需行情数据源才能在图上画出笔/线段，故这里用桩页验证面板自身逻辑；
后端接口逻辑由 ``tests/signal_monitor/test_strength_compare.py`` 的 pytest 用例覆盖。
"""
import pathlib
import shutil
import sys
import tempfile

from playwright.sync_api import sync_playwright

_REPO = pathlib.Path(__file__).resolve().parents[3]
_JS = _REPO / "web" / "chanlun_chart" / "cl_app" / "static" / "js" / "strength_compare.js"

_HARNESS_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>sc panel harness</title>
<style>body{background:#131722;margin:0;height:100vh;font-family:sans-serif;}</style>
</head><body>
<script>
  window.Utils = {
    get_market: function(){ return "a"; },
    get_local_data: function(k){ return k === "a_code" ? "SH.600519" : ""; }
  };
  window.tvWidget = {
    symbolInterval: function(){ return {symbol:"a:SH.600519", interval:"30"}; }
  };
  var _LINES = {ok:true, line_kind:"xd", lines:[
    {start:"2024-01-01 09:30:00", end:"2024-01-02 14:00:00", type:"up",   high:12.5, low:10.1, done:true},
    {start:"2024-01-02 14:00:00", end:"2024-01-03 10:30:00", type:"down", high:12.5, low:9.8,  done:true},
    {start:"2024-01-03 10:30:00", end:"2024-01-05 11:00:00", type:"up",   high:13.2, low:9.8,  done:false}
  ]};
  var _CMP = {ok:true, line_kind:"xd", direction:"up", is_beichi:true, made_new_extreme:true,
    macd_area_ratio:0.62, macd_peak_ratio:0.70, strength_score:38,
    ref:{start:"2024-01-01 09:30:00", end:"2024-01-02 14:00:00", high:12.5, low:10.1},
    cur:{start:"2024-01-03 10:30:00", end:"2024-01-05 11:00:00", high:13.2, low:9.8, done:false},
    verdict:"背驰：当前段创新极值但力度衰减"};
  window.fetch = function(url){
    var data = (String(url).indexOf("/lines/") >= 0) ? _LINES : _CMP;
    return Promise.resolve({json:function(){ return Promise.resolve(data); }});
  };
</script>
<script src="strength_compare.js"></script>
</body></html>
"""


def main() -> int:
    if not _JS.is_file():
        print(f"找不到 strength_compare.js: {_JS}")
        return 1

    work = pathlib.Path(tempfile.mkdtemp(prefix="sc_verify_"))
    try:
        shutil.copy(_JS, work / "strength_compare.js")
        harness = work / "harness.html"
        harness.write_text(_HARNESS_HTML, encoding="utf-8")

        errors = []
        results = []

        def check(name, cond):
            results.append((name, bool(cond)))

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))

            page.goto(harness.as_uri())
            page.wait_for_load_state("networkidle")

            btn = page.locator("#sc-toggle-btn")
            check("悬浮按钮存在", btn.count() == 1)
            if btn.count() == 1:
                check("按钮文字=力度对比", btn.inner_text().strip() == "力度对比")
                btn.click()
            page.wait_for_timeout(200)
            check("面板可见", page.locator("#sc-panel").is_visible())
            check("市场预填=a", page.input_value("#sc-market") == "a")
            check("代码预填=SH.600519", page.input_value("#sc-code") == "SH.600519")
            check("周期预填=30m", page.input_value("#sc-freq") == "30m")

            page.click("#sc-load")
            page.wait_for_timeout(400)
            check("线段下拉填充>=3", page.locator("#sc-ref option").count() >= 3)

            if page.locator("#sc-ref option").count() >= 2:
                page.select_option("#sc-ref", index=1)
                page.click("#sc-run")
                page.wait_for_timeout(400)
            rt = page.locator("#sc-result").inner_text()
            check("结果含背驰判定", "背驰" in rt)
            check("结果含强度分", "强度分" in rt)
            check("结果含参考段/当前段", "参考段" in rt and "当前段" in rt)
            browser.close()

        print("=== strength_compare.js 面板验证 ===")
        for name, ok in results:
            print(("  [PASS] " if ok else "  [FAIL] ") + name)
        print("控制台错误: " + ("无" if not errors else str(len(errors))))
        for e in errors:
            print("  ERR: " + e)
        ok_all = all(o for _, o in results) and not errors
        print("=== " + ("全部通过" if ok_all else "存在失败") + " ===")
        return 0 if ok_all else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
