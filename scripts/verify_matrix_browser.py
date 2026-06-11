# -*- coding: utf-8 -*-
"""浏览器端三周期矩阵验证（最后一公里）：登录 → SH.000001 1m/5m/30m → 截图 +
读取 charts.js 容器内已渲染 shape 计数（硬指标）。

跑法: PYTHONPATH=src;. python scripts/verify_matrix_browser.py [recon|verify]
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from chanlun import config as cl_config
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9900"
OUT = "D:/chanlun_pro/browser_verify"


def login(page):
    # networkidle 不可靠(页面有轮询/长计算),用 domcontentloaded+显式等元素
    page.goto(BASE + "/login?next=%2F", wait_until="domcontentloaded", timeout=60000)
    if "/login" in page.url:
        page.wait_for_selector("input[type=password]", timeout=30000)
        page.fill("input[type=password]", cl_config.LOGIN_PWD)
        # layui AJAX 提交无导航,点击后主动跳首页
        page.click("button[type=submit], input[type=submit]", no_wait_after=True)
        time.sleep(2)
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=120000)
    assert "/login" not in page.url, f"登录失败, 仍在 {page.url}"
    print("登录成功:", page.url)


def recon(page):
    page.screenshot(path=f"{OUT}/recon_home.png", full_page=False)
    btns = page.locator("button, a.button, [role=button]").all()
    print(f"== 可见控件({len(btns)}) ==")
    for b in btns[:60]:
        try:
            t = (b.inner_text() or "").strip().replace("\n", "|")[:30]
            if t:
                print(" -", t)
        except Exception:
            pass
    inputs = page.locator("input").all()
    print(f"== inputs({len(inputs)}) ==")
    for i in inputs[:10]:
        try:
            print(" -", i.get_attribute("placeholder") or i.get_attribute("type"))
        except Exception:
            pass


COUNT_JS = """() => {
  const cm = window.__cm && (window.__cm['1'] || Object.values(window.__cm)[0]);
  if (!cm || !cm.obj_charts) return {err: 'no __cm'};
  const sks = Object.keys(cm.obj_charts);
  if (!sks.length) return {err: 'no symbolKey'};
  const sk = sks[sks.length - 1];
  const t = cm.obj_charts[sk];
  const cnt = {};
  for (const [k, v] of Object.entries(t)) {
    if (Array.isArray(v) && v.length) cnt[k] = v.length;
  }
  return {sk, cnt};
}"""


def _wait_shapes(page, prev_sk, max_sec=720):
    """轮询直到 obj_charts 出现新 symbolKey(≠prev_sk)且 bis 非空。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < max_sec:
        r = page.evaluate(COUNT_JS)
        if r != last:
            print(f"  [{int(time.time()-t0)}s]", str(r)[:240], flush=True)
            last = r
        if (isinstance(r, dict) and r.get("sk") and r["sk"] != prev_sk
                and r.get("cnt", {}).get("bis")):
            return r
        time.sleep(10)
    return last


def verify(page):
    """三周期硬指标 + 截图。"""
    results = {}
    prev_sk = None
    for res, tag in [("1", "1m"), ("5", "5m"), ("30", "30m")]:
        if res != "1":
            page.evaluate(
                "r => window.tvWidget.activeChart().setResolution(r)", res)
            time.sleep(5)
        r = _wait_shapes(page, prev_sk=prev_sk, max_sec=720)
        if isinstance(r, dict):
            prev_sk = r.get("sk")
        time.sleep(12)                      # 等后续批次 reconcile(recursive_* 晚于 bis)
        r = page.evaluate(COUNT_JS)
        results[tag] = r
        page.screenshot(path=f"{OUT}/verify_{tag}.png", full_page=False)
        print(f"== {tag} 容器计数 ==", str(r)[:500], flush=True)
    import json
    print(json.dumps(results, ensure_ascii=False, indent=1)[:2500])


def probe_history(page):
    """登录会话内 fetch /tv/history,检查 recursive_levels 等字段实际内容。"""
    js = """async ([res, frm, to]) => {
      const u = `/tv/history?symbol=${encodeURIComponent('a:SH.000001')}&resolution=${res}&from=${frm}&to=${to}&firstDataRequest=true`;
      const r = await fetch(u);
      const d = await r.json();
      const out = {status: d.s, bars: (d.t||[]).length, keys: Object.keys(d).length};
      const rl = d.recursive_levels;
      out.recursive_levels = rl ? rl.map(lv => ({level: lv.level,
        zss: (lv.zss||[]).length, zslxs: (lv.zslxs||[]).length,
        mmds: (lv.mmds||[]).length, bcs: (lv.bcs||[]).length})) : null;
      for (const k of ['bis','xds','bi_zss','xd_zss','xd_mmds','xd_bcs','bi_mmds','higher_zs'])
        out[k] = Array.isArray(d[k]) ? d[k].length : String(d[k]);
      return out;
    }"""
    import json
    now = int(time.time())
    for res, days in [("1", 30), ("5", 90), ("30", 365)]:
        r = page.evaluate(js, [res, now - days * 86400, now])
        print(f"== /tv/history res={res} ==")
        print(json.dumps(r, ensure_ascii=False, indent=1)[:1200], flush=True)


def probe2(page):
    """探 cl_show_config toggle + obj_charts 全部键(含空) + barsResult 来源。"""
    time.sleep(20)
    info = page.evaluate("""() => {
      const cm = window.__cm && (window.__cm['1'] || Object.values(window.__cm)[0]);
      if (!cm) return {err: 'no cm'};
      const out = {};
      out.cfg = cm.cl_show_config;
      out.recMaxLevel = cm._recMaxLevel;
      const sks = Object.keys(cm.obj_charts || {});
      out.symbolKeys = sks;
      if (sks.length) {
        const t = cm.obj_charts[sks[sks.length-1]];
        out.allContainers = Object.fromEntries(Object.entries(t).map(
          ([k,v]) => [k, Array.isArray(v) ? v.length : typeof v]));
      }
      // datafeed/管理器上可能缓存的最近 chart 数据
      out.cmKeys = Object.keys(cm).filter(k => /bar|data|result|cache/i.test(k)).slice(0,20);
      return out;
    }""")
    import json
    print(json.dumps(info, ensure_ascii=False, indent=1)[:3000], flush=True)


def probe4(page):
    """5m 图缩放假设验证：默认窗口 vs 拉宽到 150 天后的 recursive_zss 计数。"""
    import json
    page.evaluate("r => window.tvWidget.activeChart().setResolution(r)", "5")
    r1 = _wait_shapes(page, prev_sk=None, max_sec=600)
    time.sleep(10)
    r1 = page.evaluate(COUNT_JS)
    print("默认窗口:", json.dumps(r1, ensure_ascii=False)[:300], flush=True)
    now = int(time.time())
    page.evaluate(
        "rng => window.tvWidget.activeChart().setVisibleRange(rng)",
        {"from": now - 150 * 86400, "to": now})
    time.sleep(25)
    r2 = page.evaluate(COUNT_JS)
    print("150天窗口:", json.dumps(r2, ensure_ascii=False)[:300], flush=True)
    page.screenshot(path=f"{OUT}/probe4_5m_wide.png", full_page=False)


def recon3(page):
    """dump chart_widgets/obj_charts 运行时结构。"""
    time.sleep(5)
    info = page.evaluate("""() => {
      const out = {};
      const cw = window.chart_widgets;
      out.cw_type = Object.prototype.toString.call(cw);
      try { out.cw_keys = Object.keys(cw).slice(0, 8); } catch (e) { out.cw_keys = String(e); }
      let inst = null;
      try {
        const vals = Array.isArray(cw) ? cw : Object.values(cw);
        inst = vals.find(v => v && v.obj_charts);
      } catch (e) {}
      if (inst) {
        out.symbolKeys = Object.keys(inst.obj_charts);
        const sk = out.symbolKeys[0];
        if (sk) {
          const types = inst.obj_charts[sk];
          out.containers = Object.fromEntries(
            Object.entries(types).map(([k, v]) => [k, Array.isArray(v) ? v.length : typeof v]));
        }
      } else { out.note = 'no obj_charts holder found'; }
      return out;
    }""")
    import json
    print(json.dumps(info, ensure_ascii=False, indent=1)[:3000])


def recon2(page):
    """等图表就绪后:window 关键对象 / iframe / 周期控件。"""
    for k in range(24):                       # 最多等 120s(v34 后首算慢)
        n_iframe = page.locator("iframe").count()
        spin = page.locator(".layui-layer-loading, .loading, [class*=spin]").count()
        print(f"  t={k*5}s iframes={n_iframe} spinners={spin}")
        if n_iframe > 0:
            break
        time.sleep(5)
    page.screenshot(path=f"{OUT}/recon2_chart.png", full_page=False)
    keys = page.evaluate(
        "Object.keys(window).filter(k => /chart|tv|widget|kline/i.test(k)).slice(0,30)")
    print("window keys:", keys)
    # TV widget iframe 内的周期按钮(外层也可能有自定义周期 UI)
    for f in page.frames:
        print("frame:", f.url[:100])
    # 外层周期控件候选
    for sel in ["#chart_interval", "[data-interval]", ".interval-item"]:
        c = page.locator(sel).count()
        if c:
            print("interval ctl:", sel, c)


def main():
    import os
    os.makedirs(OUT, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else "recon"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1900, "height": 1000})
        page.on("console", lambda m: m.type == "error" and print("[js-err]", m.text[:160]))
        login(page)
        time.sleep(8)                      # 等图表初始化/数据加载
        if mode == "recon":
            recon(page)
        elif mode == "recon2":
            recon2(page)
        elif mode == "recon3":
            recon3(page)
        elif mode == "verify":
            verify(page)
        elif mode == "probe":
            probe_history(page)
        elif mode == "probe2":
            probe2(page)
        elif mode == "probe4":
            probe4(page)
        browser.close()
    print("done ->", OUT)


if __name__ == "__main__":
    main()
