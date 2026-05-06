# Web 启动首屏 K 线性能优化 — 设计文档

- 日期：2026-05-07
- 范围：把 web 服务"启动到首根 K 线渲染"的耗时从 ~30s 压缩到二次启动 2-5s、首次启动 8-12s
- 性质：综合性能优化（后端静态资源 + 前端启动顺序 + 数据预热 + 资源裁剪）

---

## 1. 背景与动机

启动后用户从打开页面到第一根 K 线渲染要 ~30s。前序两轮已修复后端 stocks 缓存问题（`PRELOAD_STARTUP_DELAY_SECONDS=0` + 落盘恢复），后端首请求已能 3ms 命中缓存，但**首屏 K 线时间未变**。诊断表明真正瓶颈在前端：

```
charting_library 静态资源:    24 MB（含 18 国语言包）
  └─ library.668013b6b41ce2feaa5c.js: 2.48 MB（主 bundle）
  └─ 18 个 i18n bundles:        每个 ~200 KB
datafeeds:                    33 MB
TOTAL static:                 61 MB

Cache-Control: no-cache       ← 元凶
Content-Encoding:             ← 没启用 gzip
```

每次浏览器冷启动都得下载几 MB 文本资源；二次启动也得对几十个文件做 304 校验各 1 个 RTT；叠加 `webbrowser.open` 触发的浏览器进程冷启动 5-10s——加起来正好 ~30s。

## 2. 目标

| 场景 | 现状 | 目标 |
|---|---|---|
| 第一次启动（无 disk cache） | ~30s | 8-12s |
| 第二次启动（disk cache 命中） | ~30s | 2-5s |
| 切回上次的标的（重启后） | first=true 几百 ms | 3ms（RAM 命中）|
| 用户白屏体感 | 白屏 30s | 立即出现"加载中" |

## 3. 非目标

- 不替换 TradingView charting library（工程量大，跨业务依赖广）
- 不引入 nginx / CDN（保持单进程开发场景的简洁）
- 不实现首屏 canvas 快照占位（loading_screen 已能解决白屏焦虑，工程量不值）
- 不做浏览器 CDP 复用（边界情况多，"默认不自动开浏览器"已等价收益）
- 不修改 chart_compute / 缠论计算逻辑

## 4. 改动总览

### 4.1 后端（5 项）

| # | 改动 | 涉及文件 |
|---|---|---|
| B1 | 自定义 `CachedStaticFileHandler`，给 charting_library / datafeeds 加 immutable cache + gzip | 新增 `web/chanlun_chart/cl_app/handlers/cached_static.py`，修改 `web/chanlun_chart/app.py` |
| B2 | 启动时一次性预压缩 `static/charting_library/**`、`static/datafeeds/**` 下的 `.js`/`.css` 为 `.gz` | 新增 `web/chanlun_chart/cl_app/services/static_precompress.py`，`app.py` 启动钩子调用 |
| B3 | Tornado Application 路由：拦截 `/static/charting_library/*`、`/static/datafeeds/*` 走 CachedStaticFileHandler，剩下 fallback 给 Flask（WSGIContainer） | 修改 `app.py` 的 HTTPServer 构造 |
| B4 | `webbrowser.open` 改可控；默认关闭，`CHANLUN_AUTO_OPEN=1` 启用；启动后日志高亮打印 URL | 修改 `app.py` |
| B5 | chart_data 启动预热——把"用户最后访问的 market/code/frequency"持久化到磁盘，启动时读出并把对应磁盘冷层（`fdb.get_chart_cache`）entry 回填 RAM `chart_data_cache` | 新增 `web/chanlun_chart/cl_app/services/last_chart_state.py`，修改 `web/chanlun_chart/cl_app/blueprints/tv.py`（在 `_mark_user_request` 写状态）和 `app.py`（启动时预热） |

### 4.2 前端（2 项）

| # | 改动 | 涉及文件 |
|---|---|---|
| F1 | TV widget 配置加 `loading_screen: { backgroundColor, foregroundColor }`；容器 div 加骨架文字"图表加载中..."（widget ready 后自动覆盖） | 修改 `web/chanlun_chart/cl_app/static/js/charts.js` 的 widget 实例化处（line 598 附近） |
| F2 | JS 启动顺序：`SymbolsPanel.init()`、`AI.init_ai_opts()`、`PrewarmController.fetchStatusOnce()` 用 `requestIdleCallback`（fallback `setTimeout(..., 200)`）推到主 widget 渲染之后 | 修改 `web/chanlun_chart/cl_app/templates/index.html`（line 994、AI/Prewarm init 附近） |

### 4.3 资源裁剪（1 项）

| # | 改动 | 涉及文件 |
|---|---|---|
| R1 | 把 `static/charting_library/bundles/` 下用不到的 i18n 包移到 `bundles_unused/` 子目录（不删，方便回滚），保留 `zh_CN`、`en` 默认包 | mv 操作；新增 `web/chanlun_chart/cl_app/static/charting_library/bundles_unused/.gitkeep` |

---

## 5. 后端详细设计

### 5.1 CachedStaticFileHandler（B1）

新增 `web/chanlun_chart/cl_app/handlers/cached_static.py`：

```python
import os
from tornado.web import StaticFileHandler

class CachedStaticFileHandler(StaticFileHandler):
    """给 charting_library / datafeeds 这种 hash-named 静态资源发 immutable cache 头，
    顺带按 Accept-Encoding 分发预压缩好的 .gz 文件。

    cache 头：Cache-Control: public, max-age=31536000, immutable
        - 文件名都带 hash，永久 cache 安全
    gzip：客户端 Accept-Encoding 含 gzip 且兄弟 .gz 存在 → 透明发送 .gz +
        Content-Encoding: gzip 头。否则发送原文件。
    """

    def get_absolute_path(self, root, path):
        full = super().get_absolute_path(root, path)
        accept = self.request.headers.get("Accept-Encoding", "")
        if "gzip" in accept and os.path.isfile(full + ".gz"):
            self._serving_gz = True
            return full + ".gz"
        return full

    def set_extra_headers(self, path):
        self.set_header(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
        if getattr(self, "_serving_gz", False):
            self.set_header("Content-Encoding", "gzip")
            # 让代理/浏览器知道按 Accept-Encoding 区分缓存条目
            self.set_header("Vary", "Accept-Encoding")

    def get_content_type(self):
        # 修正：当 path 是 .js.gz 时父类会返回 application/x-gzip。
        # 我们透明发的是 js，要把原 mime 还回去。
        if getattr(self, "_serving_gz", False):
            base = self.absolute_path[:-3]  # 去掉 .gz
            ext = os.path.splitext(base)[1].lower()
            return {
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".html": "text/html; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(ext, "application/octet-stream")
        return super().get_content_type()
```

边界：
- `Vary: Accept-Encoding` 必带，避免反向代理把 gz 响应缓存后误发给不支持 gzip 的客户端
- 透明 gzip 时 `Content-Length` 由父类基于实际文件大小自动设置（即 .gz 大小），无须手动改
- `set_extra_headers` 的 path 参数是相对路径，不能用来判断是不是 .gz——所以用 instance flag `self._serving_gz`

### 5.2 静态资源预压缩（B2）

新增 `web/chanlun_chart/cl_app/services/static_precompress.py`：

```python
import gzip
import os
import time

from chanlun.tools.log_util import LogUtil

# 只压这些扩展名（二进制资源 .png/.woff 已自带压缩，再压收益小且 cpu 浪费）
_PRECOMPRESS_EXTS = (".js", ".css", ".json", ".svg", ".html", ".map")
# gzip 等级：9 启动多花 1-2s 但产出最小；6 是性能/体积平衡点。
# 启动期一次性，选 9。
_GZIP_LEVEL = 9


def precompress_directory(root: str) -> tuple[int, int, float]:
    """递归扫 root 下目标扩展名文件，给每个生成兄弟 .gz。
    返回 (compressed_count, skipped_count, elapsed_seconds)。

    跳过条件：
      - .gz 已存在且 mtime >= 源文件 mtime
      - 源文件大小 < 1024 字节（gzip 头部+meta 占 20+ 字节，小文件压不动）
    异常被吞掉 + warn，不影响启动。
    """
    if not os.path.isdir(root):
        return (0, 0, 0.0)
    t0 = time.time()
    compressed = skipped = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(_PRECOMPRESS_EXTS):
                continue
            if fn.endswith(".gz"):
                continue
            src = os.path.join(dirpath, fn)
            dst = src + ".gz"
            try:
                if os.path.getsize(src) < 1024:
                    skipped += 1
                    continue
                if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
                    skipped += 1
                    continue
                with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=_GZIP_LEVEL) as fo:
                    while True:
                        buf = fi.read(1024 * 256)
                        if not buf:
                            break
                        fo.write(buf)
                compressed += 1
            except Exception as e:
                LogUtil.warning(f"[precompress] {src} 失败: {e}")
    return (compressed, skipped, time.time() - t0)


def precompress_static_assets(static_root: str) -> None:
    """启动钩子：预压缩 charting_library / datafeeds 两个大头目录。"""
    targets = [
        os.path.join(static_root, "charting_library"),
        os.path.join(static_root, "datafeeds"),
    ]
    for t in targets:
        c, s, e = precompress_directory(t)
        LogUtil.info(
            f"[precompress] {os.path.basename(t)}: 压缩={c} 跳过={s} 耗时={e:.2f}s"
        )
```

边界：
- 第一次启动会一次性压缩 ~25MB 文本，预估 5-15s（gzip 9 级 + Python GIL 限制单核）。可接受，因为只发生一次。
- 后续启动若文件 mtime 没变（库版本没升），全部跳过，<100ms。
- 升级 charting library 时只要源文件 mtime > .gz mtime 即自动重压。

### 5.3 Tornado Application 路由（B3）

`app.py` 现状：

```python
s = HTTPServer(WSGIContainer(app, executor=ThreadPoolExecutor(http_workers)))
```

改为：

```python
from tornado.web import Application, FallbackHandler
from tornado.wsgi import WSGIContainer
from cl_app.handlers.cached_static import CachedStaticFileHandler

static_root = os.path.join(
    os.path.dirname(__file__), "cl_app", "static"
)
charting_dir = os.path.join(static_root, "charting_library")
datafeeds_dir = os.path.join(static_root, "datafeeds")

# 启动期预压缩（B2）
from cl_app.services.static_precompress import precompress_static_assets
precompress_static_assets(static_root)

wsgi_container = WSGIContainer(app, executor=ThreadPoolExecutor(http_workers))

tornado_app = Application(
    [
        (
            r"/static/charting_library/(.*)",
            CachedStaticFileHandler,
            {"path": charting_dir},
        ),
        (
            r"/static/datafeeds/(.*)",
            CachedStaticFileHandler,
            {"path": datafeeds_dir},
        ),
        (r".*", FallbackHandler, {"fallback": wsgi_container}),
    ],
)
s = HTTPServer(tornado_app)
```

边界：
- Tornado 的 path 路由按列表顺序匹配，前两条优先级高于 fallback，安全
- Flask 的 `url_for('static', filename='charting_library/...')` 生成 `/static/charting_library/...`，匹配第一条规则
- 其他 `/static/*` 资源（layui.js / app.css / dark.html 等）继续走 Flask，不变
- 没改 `s.bind()` / `s.start(1)` / IOLoop 启动顺序

### 5.4 webbrowser.open 改可控（B4）

`app.py` 当前：

```python
if len(sys.argv) >= 2 and sys.argv[1] == "nobrowser":
    pass
else:
    webbrowser.open("http://127.0.0.1:9900")
```

改为：

```python
auto_open = os.environ.get("CHANLUN_AUTO_OPEN", "0").strip()
nobrowser_flag = len(sys.argv) >= 2 and sys.argv[1] == "nobrowser"
url = f"http://127.0.0.1:9900"
if nobrowser_flag or auto_open != "1":
    LogUtil.info("")
    LogUtil.info(f">>> Web 已启动，请在浏览器访问：{url}")
    LogUtil.info('>>> 想恢复"启动后自动开浏览器"的旧行为，set CHANLUN_AUTO_OPEN=1')
    LogUtil.info("")
else:
    webbrowser.open(url)
```

边界：
- 旧的 `nobrowser` 命令行参数继续兼容
- 默认行为变化（原来开浏览器，现在不开），需在 README / 启动脚本里同步说明
- WPF launcher 模式（`wpf_launcher` 在 argv 里）仍然走 else 分支可能开浏览器——保留这个行为，因为 WPF 是 GUI 启动器，期望自动跳浏览器

### 5.5 chart_data 启动预热（B5）

#### 5.5.1 用户最后访问状态持久化

新增 `web/chanlun_chart/cl_app/services/last_chart_state.py`：

```python
import json
import os
import tempfile
import time
import threading

from chanlun import config
from chanlun.tools.log_util import LogUtil

_VERSION = 1
_LOCK = threading.Lock()  # 写状态的简单互斥（避免多个 tv_history 同时写）


def _state_path() -> str:
    base = config.get_data_path()
    return os.path.join(str(base), "cache", "last_chart_state.json")


def record_user_request(market: str, code: str, frequency: str) -> None:
    """把用户最近访问的 (market, code, frequency) 三元组写入磁盘。

    频次：每次 tv_history 入口 firstDataRequest=true 时调用。失败仅 warn。
    防抖：内部用 _last_record 短期记忆，5s 内同 3 元组不重复写。
    """
    global _last_record
    key = (market, code, frequency)
    now = time.time()
    if _last_record and _last_record[0] == key and now - _last_record[1] < 5:
        return
    _last_record = (key, now)

    payload = {
        "version": _VERSION,
        "updated_at": int(now),
        "market": market,
        "code": code,
        "frequency": frequency,
    }
    path = _state_path()
    try:
        with _LOCK:
            dir_ = os.path.dirname(path)
            os.makedirs(dir_, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".tmp", dir=dir_, delete=False
            ) as tf:
                json.dump(payload, tf, ensure_ascii=False)
                tmp = tf.name
            os.replace(tmp, path)
    except Exception as e:
        LogUtil.warning(f"[last_chart_state] 写 {path} 失败: {e}")


def load_last_state():
    """启动期读最后一次访问状态。返回 dict 或 None。"""
    path = _state_path()
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != _VERSION:
            return None
        if not all(data.get(k) for k in ("market", "code", "frequency")):
            return None
        return data
    except Exception as e:
        LogUtil.warning(f"[last_chart_state] 读 {path} 失败: {e}")
        return None


_last_record = None
```

#### 5.5.2 在 tv_history 入口调用记录

`web/chanlun_chart/cl_app/blueprints/tv.py` 的 `tv_history` 视图，在 `_mark_user_request(market, code)` 调用旁追加：

```python
if firstDataRequest == "true":
    _mark_user_request(market, code)
    # 新增：记录最后访问状态，供下次启动预热 RAM chart_data_cache
    try:
        from cl_app.services.last_chart_state import record_user_request
        record_user_request(market, code, frequency)
    except Exception:
        pass
```

#### 5.5.3 启动时预热

`app.py` 在 `start_symbol_preload_thread()` 之后追加：

```python
def _warm_chart_cache_from_disk() -> None:
    """启动期把"用户上次访问"的 chart_data 从磁盘冷层（fdb）回填 RAM 热层。

    成本：单条 entry pickle 反序列化 ~50ms，没有网络/计算。即使条件不满足
    （没历史状态、磁盘 entry 已过期）也只是没收益，不影响主流程。
    """
    from cl_app.services.last_chart_state import load_last_state
    state = load_last_state()
    if not state:
        return
    market, code, frequency = state["market"], state["code"], state["frequency"]
    try:
        from chanlun.cl_utils import query_data_options
        from cl_app.services.chart_cache import (
            _build_cache_key, _normalize_cache_entry, chart_data_cache
        )
        from chanlun.file_db import fdb
        # cl_config 用 query_data_options 默认即可——和首次 history 的 key 一致即命中
        cl_config = query_data_options
        cache_key = _build_cache_key(market, code, frequency, cl_config)
        entry = fdb.get_chart_cache(cache_key)
        if entry is None:
            LogUtil.info(f"[chart_warm] 磁盘冷层无 {market}:{code}:{frequency} entry，跳过")
            return
        normalized = _normalize_cache_entry(entry)
        if normalized:
            chart_data_cache[cache_key] = normalized
            LogUtil.info(f"[chart_warm] 已预热 {market}:{code}:{frequency} 到 RAM")
    except Exception as e:
        LogUtil.warning(f"[chart_warm] 预热失败: {e}")

# 在 start_symbol_preload_thread() 之后
_warm_chart_cache_from_disk()
```

边界：
- cl_config 用 `query_data_options` 默认值。如果用户首次 history 时 cl_config 不同，cache_key 不一致 → 不命中，仅没收益。后续可以扩展持久化 cl_config，但当前 YAGNI。
- `_build_cache_key` 等是 chart_cache 里 `_` 开头的私有函数，跨模块导入是一种 coupling，但比起绕一层 public 包装更直接。我们在 chart_cache 模块顶部 docstring 里加一行说明"被 app 启动期预热使用"。
- fdb.get_chart_cache 不存在 entry 时返回 None，安全短路。
- 异常吞下，不影响主流程。

---

## 6. 前端详细设计

### 6.1 TV widget loading_screen（F1）

`web/chanlun_chart/cl_app/static/js/charts.js` 的 widget 实例化（line ~598）：

```javascript
this.widget = window.tvWidget = new TradingView.widget({
  // ... 现有配置 ...

  // 新增：loading_screen 让 widget 内部加载阶段显示 spinner 而非空白
  loading_screen: (function () {
    // 项目主题：localStorage.tv_chart -> JSON -> theme="dark" 或缺省（亮色）
    var isDark = false;
    try {
      var t = JSON.parse(localStorage.tv_chart || '{}');
      isDark = (t.theme === 'dark');
    } catch (e) {}
    return {
      backgroundColor: isDark ? '#1e1e1e' : '#ffffff',
      foregroundColor: '#1e9fff', // 项目主蓝，深浅主题都协调
    };
  })(),
  // ...
});
```

容器 div（在 `templates/index.html` line ~52 附近的 `tv_charts_area`）渲染 widget 之前，先显示骨架文字：

```html
<div id="tv_charts_area" style="...">
  <div id="tv_charts_skeleton" style="
    display: flex; align-items: center; justify-content: center;
    height: 100%; color: #888; font-size: 14px;
  ">图表加载中...</div>
</div>
```

JS 在 widget `onChartReady` 回调里隐藏骨架：

```javascript
this.widget.onChartReady(() => {
  document.getElementById('tv_charts_skeleton')?.remove();
  // ... 现有 onChartReady 逻辑 ...
});
```

边界：
- 多图布局（vertical-2 / four 等）每个 widget 都会有 loading_screen，骨架只在最外层 area 显示一次。
- 主题适配：项目有 dark/light 切换，`backgroundColor` 应根据 `localStorage.theme` 取值。

### 6.2 JS 启动顺序优化（F2）

`templates/index.html` 现状（按 line 顺序触发）：

```javascript
$(function () {  // jQuery DOM ready
  layui.use(function () {
    SymbolsPanel.init();           // line 994
  });
});

$(function () {
  layui.use(function () {
    AI.init_ai_opts();             // line ~1251
    AI.get_ai_analyse_records();
  });

  init_web();                       // line 1367 — TV widget 创建
  // ... interval_update_rates ...
});
```

改为：

```javascript
function defer(cb) {
  if ('requestIdleCallback' in window) {
    requestIdleCallback(cb, { timeout: 1000 });
  } else {
    setTimeout(cb, 200);
  }
}

$(function () {
  // 1) 立即：TV widget 创建（关键路径）
  init_web();

  // 2) 延后：非关键 UI 初始化
  defer(() => {
    layui.use(function () {
      SymbolsPanel.init();
      AI.init_ai_opts();
      AI.get_ai_analyse_records();
    });
    interval_update_rates = setInterval(ZiXuan.stocks_update_rate(), 30000);
  });
});
```

边界：
- 原来两个独立的 `$(function(){...})` 合并成一个，确保顺序确定
- `interval_update_rates` 是 ZiXuan 自选股的定时刷新，30s 周期，延后启动 200ms 不影响
- `SymbolsPanel.init` 里的 `self.load()` 会发 `/symbols/list` 请求，延后能避免和 TV widget 的 `/tv/symbols`、`/tv/history` 抢 HTTP worker，进一步加速首屏

---

## 7. 资源裁剪详细设计（R1）

`static/charting_library/bundles/` 下 i18n 包识别规则（按文件名扫）：

- 实际 22 个 locale，每个 locale 有 2 个文件：`<locale>.938.*.js`（~150-200KB，主翻译）和 `<locale>.2578.*.js`（~120 字节，stub/manifest）
- 保留：`zh.*.js`、`en.*.js`（项目默认中文 UI；en 是 widget 默认 fallback locale）
- 移走到 `bundles_unused/`：另外 20 个 locale（`ar`、`ca_ES`、`de`、`es`、`fr`、`he_IL`、`hu_HU`、`id_ID`、`it`、`ja`、`ko`、`ms_MY`、`pl`、`pt`、`ru`、`sv`、`th`、`tr`、`vi`、`zh_TW`），共 40 个文件
- 节省 ≈ **3.5-4MB**（每 locale ~180KB × 20 ≈ 3.6MB，加 stub 文件几 KB）

操作：手工 `mv`，不写脚本（一次性、不可重入），并在 `bundles_unused/.gitkeep` 里写一行注释说明用途。

边界：
- 如果 TV widget 配置 `locale` 是被裁剪的语种，会找 404；项目当前默认中文，已知安全
- 如果未来需要恢复某语种，把对应包从 `bundles_unused/` 移回即可
- charting_library 库版本升级（解压新版到 bundles/）会重新引入所有 i18n，需要再次裁剪。在 `bundles_unused/.gitkeep` 注释里写清这个 follow-up 步骤

---

## 8. 数据流总览

```
[启动期]
  app.py
    ├─ create_app()                                # Flask 实例
    ├─ precompress_static_assets()                 # B2: .gz 预生成
    ├─ Tornado Application 路由组装                # B3
    ├─ HTTPServer.bind(9900)
    ├─ start_symbol_preload_thread()               # 已有（含磁盘恢复）
    ├─ _warm_chart_cache_from_disk()               # B5: chart_data 预热
    └─ s.start(1) + IOLoop.start()
       └─ 不再 webbrowser.open（除非 CHANLUN_AUTO_OPEN=1）  # B4

[运行期·静态资源]
  浏览器请求 /static/charting_library/library.xxx.js
    ↓
  Tornado 路由 → CachedStaticFileHandler (B1)
    ↓
  Accept-Encoding: gzip + library.xxx.js.gz 存在 → 发送 .gz + Content-Encoding: gzip
  设 Cache-Control: public, max-age=31536000, immutable
    ↓
  浏览器 disk cache 命中后：下次启动 0 网络请求

[运行期·tv_history first=true]
  → record_user_request(market, code, frequency)   # B5: 记最后访问

[下次启动]
  → _warm_chart_cache_from_disk() 读上一段记录
  → fdb.get_chart_cache(key) 反序列化
  → chart_data_cache[key] = entry
  → 用户进页面 → 第一个 first=true history → RAM 命中 3ms 返回

[运行期·前端]
  $(document).ready
    ↓
  init_web() 立即创建 TV widget         # F2
  TV widget 渲染 loading_screen          # F1
    ↓
  onChartReady → 移除骨架，渲染 K 线
    ↓
  requestIdleCallback → SymbolsPanel/AI/Prewarm 初始化  # F2
```

---

## 9. 测试与验证

### 9.1 自动化静态校验（每改一项后跑）

- `python -c "import ast; ast.parse(open(p).read())"` 对每个改动的 .py
- `python -c "from cl_app.handlers.cached_static import CachedStaticFileHandler"` 等模块级 import 无副作用

### 9.2 单元测试

新增 `tests/test_static_precompress.py`：
- 给一个 tmpdir 放 3 个文件（一个 .js > 1KB，一个 .css > 1KB，一个 .png）
- 跑 `precompress_directory`，验证 .js/.css 生成 .gz、.png 没生成
- 再跑一次，验证 skipped == 2，compressed == 0
- touch 源文件让 mtime 变新，再跑，验证 compressed == 2

新增 `tests/test_last_chart_state.py`：
- record_user_request → load_last_state 一致
- 5s 防抖测试
- version mismatch 返回 None

新增 `tests/test_cached_static_handler.py`：
- 用 tornado.testing.AsyncHTTPTestCase 启动 Application
- 请求带 `Accept-Encoding: gzip` 和不带，验证返回头与 body 大小

### 9.3 端到端验证（手测）

启动两次 web，记录关键时间戳：

| 指标 | 第一次（首次预压缩）期望 | 第二次期望 |
|---|---|---|
| `[precompress] charting_library: ...` 耗时 | 5-15s | <0.1s |
| `[stocks_cache] 从磁盘恢复 a/hk/us` | 0-2s | 0-2s |
| `[chart_warm] 已预热 ...` | 跳过（无 last state） | <0.5s |
| `开始预加载并更新所有市场的 symbols` | T+0 | T+0 |
| 浏览器手动访问 → 第一根 K 线渲染 | 8-12s | **2-5s** |
| 切回上次的标的 first=true elapsed | （没预热）几百 ms | **3ms** |

如果第二次启动 first=true 仍非 3ms，先排查：
- last_chart_state.json 有没有写出来？
- _build_cache_key 计算的 key 是否和 history 入口一致？

---

## 10. 风险与回滚

| 改动 | 风险 | 回滚 |
|---|---|---|
| B1+B3 | Tornado 路由出错导致全站 404 | git revert app.py + 删 cached_static.py |
| B2 | 磁盘空间占用 +25MB（.gz 文件） | 删 `static/**/*.gz` |
| B4 | 用户习惯了自动开浏览器 | set `CHANLUN_AUTO_OPEN=1` 恢复 |
| B5 | 预热错配 cache_key 导致 entry 永远不命中 | 不影响功能，仅没收益 |
| F1 | loading_screen 颜色与主题不搭 | 改 charts.js 里两个色值或删 loading_screen |
| F2 | requestIdleCallback 在某些浏览器有兼容问题 | fallback setTimeout 已覆盖 |
| R1 | TV locale 配置后报 404 | mv `bundles_unused/{locale}.*.js` 回 `bundles/` |

---

## 11. 不在本次范围

留给后续可能的优化方向：

- **浏览器 CDP 复用**：复杂度高、收益等价于 B4，不做
- **首屏 canvas 快照占位**：F1 已能解决白屏焦虑，不做
- **替换 charting library**：跨业务影响大
- **chart_compute 计算异步化 / 流式输出**：和缠论核心耦合深，单独立项

---

## 12. 提交计划

预期提交分组（不强制 PR 拆分，开发者按舒适度）：

1. `perf(static): cache + gzip for charting_library/datafeeds (B1+B2+B3)`
2. `perf(startup): make webbrowser.open opt-in via CHANLUN_AUTO_OPEN (B4)`
3. `perf(startup): warm chart_data_cache from disk on boot (B5)`
4. `perf(frontend): TV widget loading_screen + skeleton (F1)`
5. `perf(frontend): defer non-critical JS init via requestIdleCallback (F2)`
6. `chore(assets): move unused i18n bundles to bundles_unused/ (R1)`

每组都附验证日志/截图。
