# Web 启动首屏 K 线性能优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 web 服务"启动到首根 K 线渲染"耗时从 ~30s 压到二次启动 2-5s、首次启动 8-12s。

**Architecture:** 后端给 `charting_library/` `datafeeds/` 两个大头静态资源加 immutable cache + 启动预压缩 .gz；前端给 TV widget 加 loading_screen + 推迟非关键 JS 初始化；裁剪 20 个用不到的 i18n 包；记录用户最后访问标的并在启动时把 chart_data 从磁盘冷层预热回 RAM；默认不自动开浏览器避免冷启动开销。

**Tech Stack:** Python (Tornado 6 + Flask + WSGIContainer), pytest, JavaScript (TradingView charting library), bash 文件移动

**Spec:** `docs/superpowers/specs/2026-05-07-startup-perf-design.md`

---

## File Structure

**New files:**
- `web/chanlun_chart/cl_app/handlers/__init__.py` — 包占位
- `web/chanlun_chart/cl_app/handlers/cached_static.py` — `CachedStaticFileHandler`（B1）
- `web/chanlun_chart/cl_app/services/static_precompress.py` — 启动预压缩（B2）
- `web/chanlun_chart/cl_app/services/last_chart_state.py` — 用户最后访问状态持久化（B5 part 1）
- `web/chanlun_chart/cl_app/static/charting_library/bundles_unused/.gitkeep` — i18n 移走目的地（R1）
- `tests/test_static_precompress.py`
- `tests/test_last_chart_state.py`
- `tests/test_cached_static_handler.py`

**Modified files:**
- `web/chanlun_chart/app.py` — Tornado Application 路由组装、预压缩钩子、`webbrowser.open` opt-in、chart_data 启动预热（B1+B2+B3+B4+B5 part 3）
- `web/chanlun_chart/cl_app/blueprints/tv.py` — `tv_history` 入口写最后访问状态（B5 part 2）
- `web/chanlun_chart/cl_app/static/js/charts.js` — TV widget `loading_screen` 配置 + onChartReady 移除骨架（F1）
- `web/chanlun_chart/cl_app/templates/index.html` — 骨架占位 div + JS 启动顺序（F1+F2）

**Moved files (R1):**
- `web/chanlun_chart/cl_app/static/charting_library/bundles/{ar,ca_ES,de,es,fr,he_IL,hu_HU,id_ID,it,ja,ko,ms_MY,pl,pt,ru,sv,th,tr,vi,zh_TW}.{938,2578}.*.js` → `bundles_unused/`（共 40 个文件）

---

## Task 1: 静态资源预压缩工具（B2）

**Files:**
- Create: `web/chanlun_chart/cl_app/services/static_precompress.py`
- Test: `tests/test_static_precompress.py`

- [ ] **Step 1.1: 写失败测试**

Create `tests/test_static_precompress.py`:

```python
"""static_precompress 单元测试 (B2)。
TDD：先验证以下行为
- .js / .css > 1KB 会被压缩成 .gz
- .png 等二进制资源不被压缩
- < 1KB 文件被跳过
- 已存在且更新的 .gz 跳过
- 源文件 mtime 更新后 .gz 会被刷新
"""
import gzip
import os
import time

import pytest

from cl_app.services.static_precompress import precompress_directory


@pytest.fixture
def workdir(tmp_path):
    # 大于 1KB 的 js
    big_js = tmp_path / "big.js"
    big_js.write_text("// hello\n" * 200, encoding="utf-8")
    # 大于 1KB 的 css
    big_css = tmp_path / "site.css"
    big_css.write_text(".x{color:#000}\n" * 200, encoding="utf-8")
    # 小文件
    small = tmp_path / "small.js"
    small.write_text("ok", encoding="utf-8")
    # 二进制
    bin_ = tmp_path / "logo.png"
    bin_.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000)
    return tmp_path


def test_compresses_js_and_css(workdir):
    c, s, _ = precompress_directory(str(workdir))
    assert c == 2  # big.js + site.css
    assert (workdir / "big.js.gz").exists()
    assert (workdir / "site.css.gz").exists()
    # png 不压
    assert not (workdir / "logo.png.gz").exists()


def test_skips_small_files(workdir):
    c, s, _ = precompress_directory(str(workdir))
    # small.js 和 logo.png 各算一次跳过（small 文件 + 非目标扩展名）
    # 实现里 logo.png 因扩展名不匹配 continue 不计入 skipped
    # small.js 因 size<1024 计入 skipped
    assert s >= 1
    assert not (workdir / "small.js.gz").exists()


def test_idempotent(workdir):
    precompress_directory(str(workdir))
    c2, s2, _ = precompress_directory(str(workdir))
    assert c2 == 0
    assert s2 >= 2  # big.js + site.css 都因 mtime 一致跳过


def test_refreshes_when_source_newer(workdir):
    precompress_directory(str(workdir))
    # 让 big.js 比它的 .gz 新
    big = workdir / "big.js"
    future = time.time() + 5
    os.utime(big, (future, future))
    c, _, _ = precompress_directory(str(workdir))
    assert c == 1  # big.js 重压；site.css 未动


def test_gz_content_matches_source(workdir):
    precompress_directory(str(workdir))
    gz = workdir / "big.js.gz"
    with gzip.open(gz, "rb") as f:
        decompressed = f.read().decode("utf-8")
    assert decompressed == (workdir / "big.js").read_text(encoding="utf-8")


def test_missing_directory_returns_zero():
    c, s, e = precompress_directory("/path/does/not/exist")
    assert (c, s) == (0, 0)
    assert e == 0.0
```

- [ ] **Step 1.2: 跑测试，确认失败**

```bash
cd D:/project/chanlun-pro && pytest tests/test_static_precompress.py -v
```

Expected: 6 个测试都 ImportError 因为 `cl_app.services.static_precompress` 不存在。

- [ ] **Step 1.3: 实现模块**

Create `web/chanlun_chart/cl_app/services/static_precompress.py`:

```python
"""静态资源预压缩（B2）。

启动期一次性扫描 charting_library / datafeeds 下的 .js/.css 等文本资源，
为每个文件生成兄弟 .gz。运行时 CachedStaticFileHandler 根据 Accept-Encoding
透明分发 .gz。

设计要点：
- gzip 等级 9：启动多花 1-2s，但 .gz 体积最小（库文件长期不变，反复加载收益大）
- 跳过条件：源文件 < 1KB（gzip 头开销不划算）；已有 .gz 且 mtime >= 源文件
- 异常被吞掉 + warn，绝不影响启动主流程
"""
import gzip
import os
import time

from chanlun.tools.log_util import LogUtil

# 只压这些扩展名（图片/字体已自带压缩）
_PRECOMPRESS_EXTS = (".js", ".css", ".json", ".svg", ".html", ".map")
_GZIP_LEVEL = 9
_MIN_SIZE_BYTES = 1024


def precompress_directory(root: str) -> tuple[int, int, float]:
    """递归扫 root 下目标扩展名文件，给每个生成兄弟 .gz。

    返回 (compressed, skipped, elapsed_seconds)。
    异常被吞掉 + warn，调用方不需要 try/except。
    """
    if not os.path.isdir(root):
        return (0, 0, 0.0)
    t0 = time.time()
    compressed = 0
    skipped = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".gz"):
                continue
            if not fn.endswith(_PRECOMPRESS_EXTS):
                continue
            src = os.path.join(dirpath, fn)
            dst = src + ".gz"
            try:
                if os.path.getsize(src) < _MIN_SIZE_BYTES:
                    skipped += 1
                    continue
                if (
                    os.path.isfile(dst)
                    and os.path.getmtime(dst) >= os.path.getmtime(src)
                ):
                    skipped += 1
                    continue
                with open(src, "rb") as fi, gzip.open(
                    dst, "wb", compresslevel=_GZIP_LEVEL
                ) as fo:
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

- [ ] **Step 1.4: 跑测试，确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_static_precompress.py -v
```

Expected: 6 passed.

- [ ] **Step 1.5: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/cl_app/services/static_precompress.py tests/test_static_precompress.py
git commit -m "$(cat <<'EOF'
perf(static): 添加启动期预压缩工具 (B2)

- precompress_directory: 递归扫 .js/.css/.json 等文本资源，生成兄弟 .gz
- gzip level 9，跳过 <1KB 文件和已是最新的 .gz
- 单元测试覆盖压缩/跳过/幂等/刷新/PNG 排除/缺失目录
EOF
)"
```

---

## Task 2: CachedStaticFileHandler（B1）

**Files:**
- Create: `web/chanlun_chart/cl_app/handlers/__init__.py`
- Create: `web/chanlun_chart/cl_app/handlers/cached_static.py`
- Test: `tests/test_cached_static_handler.py`

- [ ] **Step 2.1: 写失败测试**

Create `tests/test_cached_static_handler.py`:

```python
"""CachedStaticFileHandler 集成测试 (B1)。

通过 tornado.testing.AsyncHTTPTestCase 启一个 mini Application，验证：
- 不带 Accept-Encoding：返回原文件 + immutable cache header
- 带 gzip：发 .gz 文件 + Content-Encoding: gzip + Vary: Accept-Encoding
- .gz 不存在时仍能正常发原文件
- mime type：透明发 .gz 时 Content-Type 仍是 application/javascript
"""
import gzip
import os

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from cl_app.handlers.cached_static import CachedStaticFileHandler


class CachedStaticHandlerTest(AsyncHTTPTestCase):
    def setUp(self):
        self.tmpdir = self.get_tmpdir()
        # 真 .js 文件 + 兄弟 .gz
        self.js_text = "// hello world\n" * 200
        self.js_path = os.path.join(self.tmpdir, "lib.js")
        with open(self.js_path, "w", encoding="utf-8") as f:
            f.write(self.js_text)
        with gzip.open(self.js_path + ".gz", "wb") as f:
            f.write(self.js_text.encode("utf-8"))
        # 一个无 .gz 的小文件
        self.css_path = os.path.join(self.tmpdir, "site.css")
        with open(self.css_path, "w", encoding="utf-8") as f:
            f.write(".x{}")
        super().setUp()

    def get_tmpdir(self):
        import tempfile
        return tempfile.mkdtemp(prefix="cached_static_test_")

    def get_app(self):
        return Application(
            [(r"/(.*)", CachedStaticFileHandler, {"path": self.tmpdir})]
        )

    def test_no_accept_encoding_returns_plain(self):
        resp = self.fetch("/lib.js", method="GET")
        assert resp.code == 200
        assert resp.body.decode("utf-8") == self.js_text
        assert "gzip" not in resp.headers.get("Content-Encoding", "")
        cache = resp.headers.get("Cache-Control", "")
        assert "immutable" in cache
        assert "max-age=31536000" in cache

    def test_gzip_accept_returns_gz(self):
        resp = self.fetch(
            "/lib.js",
            method="GET",
            headers={"Accept-Encoding": "gzip"},
            decompress_response=False,
        )
        assert resp.code == 200
        assert resp.headers.get("Content-Encoding") == "gzip"
        assert "Accept-Encoding" in resp.headers.get("Vary", "")
        # body 是 gzip 压缩后的
        assert gzip.decompress(resp.body).decode("utf-8") == self.js_text
        # mime 是 js，不是 application/x-gzip
        ct = resp.headers.get("Content-Type", "")
        assert "javascript" in ct.lower()

    def test_gzip_falls_back_when_gz_missing(self):
        # site.css 没生成 .gz，即使 client 请求 gzip 也回退原文件
        resp = self.fetch(
            "/site.css",
            method="GET",
            headers={"Accept-Encoding": "gzip"},
            decompress_response=False,
        )
        assert resp.code == 200
        assert resp.headers.get("Content-Encoding", "") == ""
        assert resp.body == b".x{}"

    def test_404_for_missing_file(self):
        resp = self.fetch("/nope.js", method="GET")
        assert resp.code == 404
```

- [ ] **Step 2.2: 跑测试，确认失败**

```bash
cd D:/project/chanlun-pro && pytest tests/test_cached_static_handler.py -v
```

Expected: 4 ImportError on `cl_app.handlers.cached_static`.

- [ ] **Step 2.3: 实现 handlers 包**

Create `web/chanlun_chart/cl_app/handlers/__init__.py`:

```python
"""自定义 Tornado handlers（cl_app 的 web 层扩展）。"""
```

Create `web/chanlun_chart/cl_app/handlers/cached_static.py`:

```python
"""CachedStaticFileHandler（B1）。

给 charting_library / datafeeds 这种 hash-named 静态资源发 immutable cache
头，并按 Accept-Encoding 透明分发预压缩好的 .gz 文件。

  - Cache-Control: public, max-age=31536000, immutable
      文件名都带 hash（library.668013b6b41ce2feaa5c.js），永久 cache 安全
  - Content-Encoding: gzip + Vary: Accept-Encoding
      仅当客户端支持 gzip 且兄弟 .gz 存在时启用
  - Content-Type 修正
      Tornado 父类对 .js.gz 会返回 application/x-gzip；我们透明发的是 .js，
      要把 mime 还回去
"""
import os

from tornado.web import StaticFileHandler


_MIME_BY_EXT = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".map": "application/json; charset=utf-8",
}


class CachedStaticFileHandler(StaticFileHandler):
    def get_absolute_path(self, root, path):
        full = super().get_absolute_path(root, path)
        accept = self.request.headers.get("Accept-Encoding", "")
        if "gzip" in accept and os.path.isfile(full + ".gz"):
            self._serving_gz = True
            return full + ".gz"
        self._serving_gz = False
        return full

    def set_extra_headers(self, path):
        self.set_header(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
        if getattr(self, "_serving_gz", False):
            self.set_header("Content-Encoding", "gzip")
            # 让代理/浏览器按 Accept-Encoding 区分缓存条目
            self.set_header("Vary", "Accept-Encoding")

    def get_content_type(self):
        if getattr(self, "_serving_gz", False):
            base = self.absolute_path[:-3]  # 去掉 .gz
            ext = os.path.splitext(base)[1].lower()
            return _MIME_BY_EXT.get(ext, "application/octet-stream")
        return super().get_content_type()
```

- [ ] **Step 2.4: 跑测试，确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_cached_static_handler.py -v
```

Expected: 4 passed.

- [ ] **Step 2.5: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/cl_app/handlers/__init__.py web/chanlun_chart/cl_app/handlers/cached_static.py tests/test_cached_static_handler.py
git commit -m "$(cat <<'EOF'
perf(static): 添加 CachedStaticFileHandler (B1)

- 给 hash-named 静态资源发 Cache-Control: public, max-age=31536000, immutable
- 透明 gzip：客户端支持 gzip 且兄弟 .gz 存在时发送 .gz + Content-Encoding
- 修正 mime type，避免 .js.gz 被识别成 application/x-gzip
EOF
)"
```

---

## Task 3: app.py 接入静态资源处理（B2 钩子 + B3 路由）

**Files:**
- Modify: `web/chanlun_chart/app.py`

- [ ] **Step 3.1: 备份并修改 main() 中的 server 构造**

打开 `web/chanlun_chart/app.py`，定位到 line 79 附近：

```python
        s = HTTPServer(WSGIContainer(app, executor=ThreadPoolExecutor(http_workers)))
        s.bind(9900, config.WEB_HOST)
```

替换为：

```python
        # B2: 启动期预压缩 charting_library / datafeeds 下的 .js/.css 为 .gz。
        # 第一次启动会多 5-15s（gzip level 9 单核），之后凭 mtime 跳过。
        from cl_app.services.static_precompress import precompress_static_assets
        static_root = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "cl_app", "static",
        )
        precompress_static_assets(static_root)

        # B1+B3: Tornado Application 路由——charting_library / datafeeds 走自定义
        # CachedStaticFileHandler（immutable cache + 透明 gzip），其余 fallback Flask
        from tornado.web import Application, FallbackHandler
        from cl_app.handlers.cached_static import CachedStaticFileHandler

        wsgi_container = WSGIContainer(
            app, executor=ThreadPoolExecutor(http_workers)
        )
        tornado_app = Application(
            [
                (
                    r"/static/charting_library/(.*)",
                    CachedStaticFileHandler,
                    {"path": os.path.join(static_root, "charting_library")},
                ),
                (
                    r"/static/datafeeds/(.*)",
                    CachedStaticFileHandler,
                    {"path": os.path.join(static_root, "datafeeds")},
                ),
                (r".*", FallbackHandler, {"fallback": wsgi_container}),
            ]
        )
        s = HTTPServer(tornado_app)
        s.bind(9900, config.WEB_HOST)
```

- [ ] **Step 3.2: 静态校验语法**

```bash
cd D:/project/chanlun-pro && python -c "import ast; ast.parse(open('web/chanlun_chart/app.py', encoding='utf-8').read()); print('SYNTAX OK')"
```

Expected: `SYNTAX OK`.

- [ ] **Step 3.3: 启动一次 web 验证**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/app.py nobrowser
```

观察日志中应出现（首次启动）：

```
[precompress] charting_library: 压缩=N 跳过=M 耗时=X.XXs
[precompress] datafeeds: 压缩=N 跳过=M 耗时=X.XXs
HTTP 线程池容量: 32
启动成功
```

启动后**新开一个终端**验证：

```bash
curl -sI http://127.0.0.1:9900/static/charting_library/charting_library.standalone.js | grep -iE 'cache-control|content-encoding|content-type'
curl -sI -H 'Accept-Encoding: gzip' http://127.0.0.1:9900/static/charting_library/charting_library.standalone.js | grep -iE 'cache-control|content-encoding|vary|content-type'
```

Expected：
- 不带 gzip：`Cache-Control: public, max-age=31536000, immutable`、`Content-Type: application/javascript; charset=utf-8`，无 Content-Encoding
- 带 gzip：上述头 + `Content-Encoding: gzip`、`Vary: Accept-Encoding`

Ctrl+C 结束服务。

- [ ] **Step 3.4: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/app.py
git commit -m "$(cat <<'EOF'
perf(static): 接入预压缩 + Tornado 路由分发 (B2+B3)

- 启动期调 precompress_static_assets，给 charting_library/datafeeds 生成 .gz
- HTTPServer 从单一 WSGIContainer 改为 Application 路由：
  - /static/charting_library/* + /static/datafeeds/* → CachedStaticFileHandler
  - 其余 fallback 给 Flask
EOF
)"
```

---

## Task 4: webbrowser.open 改可控（B4）

**Files:**
- Modify: `web/chanlun_chart/app.py`

- [ ] **Step 4.1: 修改启动后的浏览器逻辑**

打开 `web/chanlun_chart/app.py`，定位到 line 110-113 附近：

```python
        if len(sys.argv) >= 2 and sys.argv[1] == "nobrowser":
            pass
        else:
            webbrowser.open("http://127.0.0.1:9900")
```

替换为：

```python
        # B4: 默认不自动开浏览器，避免每次启动浏览器进程冷启动 5-10s。
        # set CHANLUN_AUTO_OPEN=1 恢复旧行为；或保留旧的 nobrowser 命令行兼容。
        url = "http://127.0.0.1:9900"
        auto_open = os.environ.get("CHANLUN_AUTO_OPEN", "0").strip()
        nobrowser_flag = len(sys.argv) >= 2 and sys.argv[1] == "nobrowser"
        # WPF launcher 是 GUI 启动器，期望自动跳浏览器，保留它的旧行为
        is_wpf = "wpf_launcher" in sys.argv
        if not nobrowser_flag and (auto_open == "1" or is_wpf):
            webbrowser.open(url)
        else:
            LogUtil.info("")
            LogUtil.info(f">>> Web 已启动，请在浏览器访问：{url}")
            LogUtil.info('>>> 想恢复"启动后自动开浏览器"，set CHANLUN_AUTO_OPEN=1')
            LogUtil.info("")
```

- [ ] **Step 4.2: 静态校验**

```bash
cd D:/project/chanlun-pro && python -c "import ast; ast.parse(open('web/chanlun_chart/app.py', encoding='utf-8').read()); print('SYNTAX OK')"
```

Expected: `SYNTAX OK`.

- [ ] **Step 4.3: 手测：默认不开浏览器**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/app.py
```

观察日志末尾应该有：

```
>>> Web 已启动，请在浏览器访问：http://127.0.0.1:9900
>>> 想恢复"启动后自动开浏览器"，set CHANLUN_AUTO_OPEN=1
```

且**没有**自动开浏览器。Ctrl+C 结束。

- [ ] **Step 4.4: 手测：CHANLUN_AUTO_OPEN=1 恢复旧行为**

```bash
cd D:/project/chanlun-pro && CHANLUN_AUTO_OPEN=1 python web/chanlun_chart/app.py
```

应自动打开浏览器到 http://127.0.0.1:9900。Ctrl+C 结束。

- [ ] **Step 4.5: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/app.py
git commit -m "$(cat <<'EOF'
perf(startup): 默认不自动开浏览器 (B4)

- 默认行为变化：启动后打印 URL 让用户在已开浏览器访问
- CHANLUN_AUTO_OPEN=1 恢复旧行为（自动 webbrowser.open）
- 兼容 nobrowser 命令行参数与 WPF launcher 模式
EOF
)"
```

---

## Task 5: last_chart_state 持久化（B5 part 1）

**Files:**
- Create: `web/chanlun_chart/cl_app/services/last_chart_state.py`
- Test: `tests/test_last_chart_state.py`

- [ ] **Step 5.1: 写失败测试**

Create `tests/test_last_chart_state.py`:

```python
"""last_chart_state 单元测试 (B5 part 1)。

覆盖：
- record + load round-trip
- 5s 防抖（同三元组 5s 内不重复写）
- version 不匹配返回 None
- 缺字段返回 None
- 文件损坏返回 None
"""
import json
import os
import time

import pytest


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """把 chanlun.config.get_data_path 重定向到 tmp_path。"""
    import chanlun.config as cfg
    monkeypatch.setattr(cfg, "get_data_path", lambda: tmp_path)
    # last_chart_state 模块内部用 _last_record 全局变量做防抖，每个测试要重置
    from cl_app.services import last_chart_state as mod
    mod._last_record = None
    return tmp_path


def test_round_trip(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        record_user_request, load_last_state
    )
    record_user_request("a", "SH.000001", "1m")
    state = load_last_state()
    assert state is not None
    assert state["market"] == "a"
    assert state["code"] == "SH.000001"
    assert state["frequency"] == "1m"


def test_load_when_no_file(isolated_data_dir):
    from cl_app.services.last_chart_state import load_last_state
    assert load_last_state() is None


def test_debounce_5_seconds(isolated_data_dir, monkeypatch):
    from cl_app.services.last_chart_state import (
        record_user_request, load_last_state, _state_path
    )
    # 第一次写
    record_user_request("a", "AA", "1m")
    state1 = load_last_state()
    ts1 = state1["updated_at"]
    # 立即第二次同三元组应该被防抖
    time.sleep(0.05)
    record_user_request("a", "AA", "1m")
    state2 = load_last_state()
    assert state2["updated_at"] == ts1  # 没更新
    # 不同三元组立即生效
    record_user_request("hk", "BB", "5m")
    state3 = load_last_state()
    assert state3["code"] == "BB"


def test_version_mismatch_returns_none(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        load_last_state, _state_path
    )
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 999, "market": "a", "code": "x", "frequency": "1m"}, f)
    assert load_last_state() is None


def test_missing_field_returns_none(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        load_last_state, _state_path
    )
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "market": "a"}, f)  # 缺 code/frequency
    assert load_last_state() is None


def test_corrupt_file_returns_none(isolated_data_dir):
    from cl_app.services.last_chart_state import (
        load_last_state, _state_path
    )
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("not-json{{{")
    assert load_last_state() is None
```

- [ ] **Step 5.2: 跑测试，确认失败**

```bash
cd D:/project/chanlun-pro && pytest tests/test_last_chart_state.py -v
```

Expected: 6 ImportError.

- [ ] **Step 5.3: 实现模块**

Create `web/chanlun_chart/cl_app/services/last_chart_state.py`:

```python
"""用户最后访问的 (market, code, frequency) 三元组持久化（B5 part 1）。

启动期 chart_data 预热依赖这份记录知道用户上次在看什么标的，把对应的
chart_cache_disk entry 提前回填 RAM 热层。

存储：${DATA_PATH}/cache/last_chart_state.json
格式：{ "version": 1, "updated_at": <ts>, "market", "code", "frequency" }

防抖：tv_history 在 firstDataRequest=true 时调，但 TV widget 切周期/标的可能
触发连续多次 first=true，5s 内同三元组不重复写盘。
"""
import json
import os
import tempfile
import threading
import time

from chanlun import config
from chanlun.tools.log_util import LogUtil

_VERSION = 1
_LOCK = threading.Lock()
# 内存防抖：((market, code, frequency), monotonic_ts)
_last_record = None
_DEBOUNCE_SECONDS = 5.0


def _state_path() -> str:
    base = config.get_data_path()
    return os.path.join(str(base), "cache", "last_chart_state.json")


def record_user_request(market: str, code: str, frequency: str) -> None:
    """记录用户最近访问的三元组。失败仅 warn 不抛。"""
    global _last_record
    key = (market, code, frequency)
    now = time.time()
    if (
        _last_record is not None
        and _last_record[0] == key
        and now - _last_record[1] < _DEBOUNCE_SECONDS
    ):
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
                mode="w",
                encoding="utf-8",
                suffix=".tmp",
                dir=dir_,
                delete=False,
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
        if not isinstance(data, dict):
            return None
        if data.get("version") != _VERSION:
            return None
        if not all(data.get(k) for k in ("market", "code", "frequency")):
            return None
        return data
    except Exception as e:
        LogUtil.warning(f"[last_chart_state] 读 {path} 失败: {e}")
        return None
```

- [ ] **Step 5.4: 跑测试，确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_last_chart_state.py -v
```

Expected: 6 passed.

- [ ] **Step 5.5: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/cl_app/services/last_chart_state.py tests/test_last_chart_state.py
git commit -m "$(cat <<'EOF'
perf(startup): 添加 last_chart_state 持久化模块 (B5 part 1)

- record_user_request / load_last_state，5s 防抖避免连续切周期写盘风暴
- 原子写：tmp + os.replace；version + 字段完整性校验
- 单元测试覆盖 round-trip / 防抖 / 损坏 / 缺字段 / 版本不匹配
EOF
)"
```

---

## Task 6: tv_history 入口写最后访问状态（B5 part 2）

**Files:**
- Modify: `web/chanlun_chart/cl_app/blueprints/tv.py`

- [ ] **Step 6.1: 在 _mark_user_request 调用后追加状态记录**

打开 `web/chanlun_chart/cl_app/blueprints/tv.py`，定位到 `tv_history()` 视图里的 `_mark_user_request(market, code)` 调用（line ~726 附近）：

```python
        if firstDataRequest == "true":
            _mark_user_request(market, code)
```

改为：

```python
        if firstDataRequest == "true":
            _mark_user_request(market, code)
            # B5: 记录最后访问状态，供下次启动预热 RAM chart_data_cache。
            # 失败吞异常——这是观测/优化用，不能影响 history 主流程。
            try:
                from cl_app.services.last_chart_state import record_user_request
                record_user_request(market, code, frequency)
            except Exception:
                pass
```

- [ ] **Step 6.2: 静态校验**

```bash
cd D:/project/chanlun-pro && python -c "import ast; ast.parse(open('web/chanlun_chart/cl_app/blueprints/tv.py', encoding='utf-8').read()); print('SYNTAX OK')"
```

Expected: `SYNTAX OK`.

- [ ] **Step 6.3: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/cl_app/blueprints/tv.py
git commit -m "$(cat <<'EOF'
perf(startup): tv_history 入口记录最后访问状态 (B5 part 2)

只在 firstDataRequest=true（用户主动切标的/周期）时调 record_user_request，
避免每 3s 一次的 TradingView polling 也写盘。异常吞掉。
EOF
)"
```

---

## Task 7: chart_data 启动预热（B5 part 3）

**Files:**
- Modify: `web/chanlun_chart/app.py`

- [ ] **Step 7.1: 在 main() 中追加启动预热函数**

打开 `web/chanlun_chart/app.py`，找到 `start_symbol_preload_thread()` 调用（line ~84 附近）：

```python
        # 先启动 symbol 预加载后台线程：daemon 线程，不阻塞主流程，
        # 让其与 HTTP 服务启动并行，争取在用户首次发起请求前完成首轮缓存填充。
        start_symbol_preload_thread()
```

在它**之后**追加：

```python
        # B5: chart_data 启动预热——把"用户上次访问"的 chart_data entry 从
        # 磁盘冷层（fdb）回填 RAM 热层。这样用户首个 first=true history 请求
        # 直接 RAM 命中（~3ms）而不是落盘读（~50-100ms）。
        # 失败/无历史状态都是空操作，不影响主流程。
        try:
            _warm_chart_cache_from_disk()
        except Exception as e:
            LogUtil.warning(f"[chart_warm] 启动预热未执行: {e}")
```

然后在 `main()` 函数**之外**（顶层 def 区域，紧挨在 `def main()` 上方）添加辅助函数：

```python
def _warm_chart_cache_from_disk() -> None:
    """启动期 chart_data 预热。把上次访问的 entry 从 fdb 回填 RAM。

    cache_key 由 (market, code, frequency, hash(cl_config)) 组成；这里用
    chanlun.cl_utils.query_data_options 默认值——绝大多数用户没改默认配置，
    key 一致即命中。如果用户改了配置，仅没收益，不影响功能。
    """
    from cl_app.services.last_chart_state import load_last_state
    state = load_last_state()
    if not state:
        return
    market = state["market"]
    code = state["code"]
    frequency = state["frequency"]

    from chanlun.cl_utils import query_data_options
    from cl_app.services.chart_cache import (
        _build_cache_key,
        _normalize_cache_entry,
        chart_data_cache,
    )
    from chanlun.file_db import fdb

    cache_key = _build_cache_key(market, code, frequency, query_data_options)
    try:
        disk_entry = fdb.get_chart_cache(cache_key)
    except Exception as e:
        LogUtil.warning(
            f"[chart_warm] 读磁盘 entry 失败 key={cache_key} err={e}"
        )
        return
    if disk_entry is None:
        LogUtil.info(
            f"[chart_warm] 磁盘冷层无 {market}:{code}:{frequency} entry，跳过"
        )
        return
    normalized = _normalize_cache_entry(disk_entry)
    if normalized is None:
        return
    chart_data_cache[cache_key] = normalized
    LogUtil.info(
        f"[chart_warm] 已预热 {market}:{code}:{frequency} 到 RAM"
    )
```

- [ ] **Step 7.2: 静态校验**

```bash
cd D:/project/chanlun-pro && python -c "import ast; ast.parse(open('web/chanlun_chart/app.py', encoding='utf-8').read()); print('SYNTAX OK')"
```

Expected: `SYNTAX OK`.

- [ ] **Step 7.3: 启动一次手测**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/app.py nobrowser
```

如果项目里还没产生过 `cache/last_chart_state.json`，应看到：

```
[stocks_cache] 从磁盘恢复 a stocks，共 7390 条
... (其他启动日志) ...
启动成功
```

并**没有** `[chart_warm]` 日志（因为 last_chart_state 没文件，return 早退）。

启动后用浏览器访问，切到任意标的，让 `tv_history first=true` 触发一次 → 写出 last_chart_state.json。Ctrl+C 关闭服务。

第二次启动同样命令，应看到：

```
[chart_warm] 已预热 <market>:<code>:<freq> 到 RAM
```

或 `[chart_warm] 磁盘冷层无 ... entry，跳过`（如果该标的之前没真正缓存过 chart_data）。

- [ ] **Step 7.4: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/app.py
git commit -m "$(cat <<'EOF'
perf(startup): 启动期把上次访问的 chart_data 预热到 RAM (B5 part 3)

- _warm_chart_cache_from_disk 读 last_chart_state，把对应 fdb entry
  填回 chart_data_cache，省下首请求的 50-100ms 磁盘读
- 失败/无状态都静默跳过，不影响启动
EOF
)"
```

---

## Task 8: TV widget loading_screen + 骨架占位（F1）

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/js/charts.js`
- Modify: `web/chanlun_chart/cl_app/templates/index.html`

- [ ] **Step 8.1: 给 TV widget 加 loading_screen 配置**

打开 `web/chanlun_chart/cl_app/static/js/charts.js`，定位到 line ~598 的 widget 实例化处：

```javascript
        this.widget = window.tvWidget = new TradingView.widget({
```

在 `new TradingView.widget({` 这个对象的字段里（任选一个不冲突的位置，例如靠近 `library_path` / `theme` 旁边）插入：

```javascript
            // F1: loading_screen 让 widget 内部加载阶段显示 spinner 而非空白
            loading_screen: (function () {
                var isDark = false;
                try {
                    var t = JSON.parse(localStorage.tv_chart || '{}');
                    isDark = (t.theme === 'dark');
                } catch (e) {}
                return {
                    backgroundColor: isDark ? '#1e1e1e' : '#ffffff',
                    foregroundColor: '#1e9fff',
                };
            })(),
```

- [ ] **Step 8.2: 在容器 div 加骨架文字**

打开 `web/chanlun_chart/cl_app/templates/index.html`，定位到 `id="tv_charts_area"` 的 div（line ~46-52 附近）：

```html
          <div
            id="tv_charts_area"
            style="
              width: 100%;
              height: 100%;
              display: flex;
              flex-direction: column;
            "></div>
```

改为：

```html
          <div
            id="tv_charts_area"
            style="
              width: 100%;
              height: 100%;
              display: flex;
              flex-direction: column;
            ">
            <div
              id="tv_charts_skeleton"
              style="
                display: flex; align-items: center; justify-content: center;
                height: 100%; color: #888; font-size: 14px;
                font-family: -apple-system, sans-serif;
              ">
              图表加载中...
            </div>
          </div>
```

- [ ] **Step 8.3: 在 widget onChartReady 里移除骨架**

回到 `web/chanlun_chart/cl_app/static/js/charts.js`，定位到 line ~784 附近：

```javascript
        this.widget.onChartReady(() => {
```

在 `onChartReady` 回调**首行**追加：

```javascript
        this.widget.onChartReady(() => {
            // F1: 移除骨架占位（首屏 widget 就绪后立即清掉）
            var sk = document.getElementById('tv_charts_skeleton');
            if (sk) sk.remove();
```

- [ ] **Step 8.4: 手测**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/app.py nobrowser
```

浏览器访问 `http://127.0.0.1:9900` → 验证：
- 页面打开后立即看到"图表加载中..."文字（而不是白屏）
- TV widget 加载阶段显示其内置 spinner
- K 线渲染后骨架消失

Ctrl+C 结束。

- [ ] **Step 8.5: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/cl_app/static/js/charts.js web/chanlun_chart/cl_app/templates/index.html
git commit -m "$(cat <<'EOF'
perf(frontend): TV widget loading_screen + 骨架占位 (F1)

- widget 配置加 loading_screen，按 localStorage.tv_chart.theme 选深浅色
- 容器 div 加 #tv_charts_skeleton "图表加载中..."；onChartReady 移除
- 解决用户感知的"白屏 30s"问题，体感大幅提升
EOF
)"
```

---

## Task 9: JS 启动顺序优化（F2）

**Files:**
- Modify: `web/chanlun_chart/cl_app/templates/index.html`

- [ ] **Step 9.1: 合并两段 jQuery ready，把非关键初始化推到 idle**

打开 `web/chanlun_chart/cl_app/templates/index.html`。当前结构是两段独立 `$(function(){...})`：第一段（line ~994 附近）调 `SymbolsPanel.init()`；第二段（line ~1245-1369 附近）调 `init_web()` + `AI.init_ai_opts()` + `interval_update_rates`。

定位到第一段的 `$(function () { ... layui.use(function () { ... SymbolsPanel.init(); ... }); });` 整段，先用注释**关掉**它（保留注释作为找回原入口的线索）：

```javascript
      // F2: SymbolsPanel.init() 已挪到下方 init_web() 之后用 requestIdleCallback 延后启动
      // $(function () { layui.use(function () { ... SymbolsPanel.init(); }); });
```

然后定位到第二段（含 `init_web()` 调用，line ~1367 附近）：

```javascript
      init_web();
      interval_update_rates = setInterval(ZiXuan.stocks_update_rate(), 30000);
    });
  </script>
```

替换为：

```javascript
      // F2: 关键路径——立即创建 TV widget
      init_web();

      // F2: 非关键 UI 初始化推到主 widget 后续渲染窗口，避免抢首屏 HTTP worker
      var __defer = (window.requestIdleCallback
        ? function (cb) { window.requestIdleCallback(cb, { timeout: 1000 }); }
        : function (cb) { setTimeout(cb, 200); });
      __defer(function () {
        try {
          layui.use(function () {
            if (typeof SymbolsPanel !== 'undefined') {
              SymbolsPanel.init();
            }
          });
        } catch (e) { console.warn('SymbolsPanel.init defer error', e); }
        try {
          interval_update_rates = setInterval(ZiXuan.stocks_update_rate(), 30000);
        } catch (e) { console.warn('stocks_update_rate defer error', e); }
      });
    });
  </script>
```

注意：`AI.init_ai_opts()` / `AI.get_ai_analyse_records()` 已经在第二段 `$(function(){...})` 的更上方调用（line ~1251）；它们是依赖 `layui.use` 的，已经异步——在 init_web() 之前的代码段保持原顺序不动，避免破坏依赖。本次只把 `SymbolsPanel.init` 和 `interval_update_rates` 这两个真正阻塞 first paint 的点推后。

- [ ] **Step 9.2: 手测**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/app.py nobrowser
```

浏览器访问，验证：
- 页面打开后骨架"图表加载中..."立即出现
- TV widget 加载完成，K 线渲染
- 之后（200-1000ms 内）右侧自选股面板才出现内容
- 切标的、键盘上下、自选股等功能正常

打开 F12 Console，应看不到错误。

Ctrl+C 结束。

- [ ] **Step 9.3: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/cl_app/templates/index.html
git commit -m "$(cat <<'EOF'
perf(frontend): 推迟 SymbolsPanel/ZiXuan 启动到主 widget 之后 (F2)

把第一段独立 \$(document).ready 的 SymbolsPanel.init() 合并进主入口，
连同 interval_update_rates 用 requestIdleCallback 延后启动（fallback
setTimeout 200ms），避免首屏阶段抢 HTTP worker 影响 widget 渲染。
EOF
)"
```

---

## Task 10: i18n 资源裁剪（R1）

**Files:**
- Create: `web/chanlun_chart/cl_app/static/charting_library/bundles_unused/.gitkeep`
- Move: 40 个 i18n 文件

- [ ] **Step 10.1: 创建移走目的地 + .gitkeep 说明**

```bash
cd D:/project/chanlun-pro
mkdir -p web/chanlun_chart/cl_app/static/charting_library/bundles_unused
```

Create `web/chanlun_chart/cl_app/static/charting_library/bundles_unused/.gitkeep`:

```
此目录存放从 ../bundles/ 移走的、项目用不到的 TradingView i18n 包。
保留下来便于回滚（mv 回 bundles/ 即可）。

裁剪原因（spec 见 docs/superpowers/specs/2026-05-07-startup-perf-design.md §7）：
- charting_library 共 22 个 locale，每 locale 2 个文件（.938.*.js + .2578.*.js）
- 每个 locale ~180KB，20 个 locale ≈ 3.6MB 静态资源
- 项目只用 zh / en，其余的浏览器加载只是浪费

升级 charting library 时（解压新版到 bundles/），需要重新执行裁剪：
  cd web/chanlun_chart/cl_app/static/charting_library/bundles
  for L in ar ca_ES de es fr he_IL hu_HU id_ID it ja ko ms_MY pl pt ru sv th tr vi zh_TW; do
    git mv "$L".938.*.js ../bundles_unused/ 2>/dev/null || true
    git mv "$L".2578.*.js ../bundles_unused/ 2>/dev/null || true
  done
```

- [ ] **Step 10.2: 移走 20 个 locale × 2 文件**

```bash
cd D:/project/chanlun-pro/web/chanlun_chart/cl_app/static/charting_library/bundles
for L in ar ca_ES de es fr he_IL hu_HU id_ID it ja ko ms_MY pl pt ru sv th tr vi zh_TW; do
  git mv "$L".938.*.js ../bundles_unused/ 2>/dev/null || echo "skip $L.938"
  git mv "$L".2578.*.js ../bundles_unused/ 2>/dev/null || echo "skip $L.2578"
done
```

- [ ] **Step 10.3: 验证移动结果**

```bash
cd D:/project/chanlun-pro/web/chanlun_chart/cl_app/static/charting_library/bundles
ls | grep -E '^(zh|en)\.' | wc -l
ls ../bundles_unused/ | wc -l
```

Expected：
- 第一条输出 4（zh + en 各 2 个文件）
- 第二条输出 41（40 个 locale 文件 + 1 个 .gitkeep）

- [ ] **Step 10.4: 启动并访问页面，看浏览器是否 404**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/app.py nobrowser
```

浏览器访问，F12 → Network 面板，刷新一次，确认：
- 没有 4xx/5xx 状态码请求
- TV widget 正常渲染 K 线
- 切标的、切周期、缩放正常

Ctrl+C 结束。

- [ ] **Step 10.5: 提交**

```bash
cd D:/project/chanlun-pro
git add web/chanlun_chart/cl_app/static/charting_library/bundles_unused/
git commit -m "$(cat <<'EOF'
chore(assets): 裁剪用不到的 i18n 包到 bundles_unused/ (R1)

- 移走 20 个 locale × 2 文件 = 40 个 i18n bundle 到 bundles_unused/
- 保留 zh + en（项目默认中文 UI；en 是 widget fallback locale）
- 节省 ~3.6MB 静态资源；浏览器冷启动加载减负
- bundles_unused/.gitkeep 说明回滚和升级再裁剪步骤
EOF
)"
```

---

## Task 11: 端到端验证

**Goal:** 在裸机/无 disk cache 与有 disk cache 两种情况下，记录关键时间戳并对比预期。

- [ ] **Step 11.1: 清掉浏览器对 127.0.0.1:9900 的所有缓存**

打开 Chrome → 设置 → 隐私 → 清除浏览数据 → 时间范围"全部"，**只勾**"缓存的图片和文件"和"Cookie 及其他网站数据"，地址栏过滤 `127.0.0.1` → 清除。

或：用一个全新的浏览器 profile / 隐身窗口。

- [ ] **Step 11.2: 第一次启动（首次预压缩 + 无浏览器 cache）**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/app.py nobrowser
```

记录 web server 日志中以下时间戳（相对启动）：

| 标记 | 期望时间 |
|---|---|
| `[precompress] charting_library: ...` | 5-15s（首次） |
| `[precompress] datafeeds: ...` | 5-15s（首次） |
| `启动成功` | 上面之后立即 |
| `[stocks_cache] 从磁盘恢复 ...` 或 `市场 ... 预加载完成` | 0-2s（取决于是否有上次的 stocks 文件） |
| `[chart_warm] 已预热 ...` 或 跳过 | <1s |

浏览器访问 `http://127.0.0.1:9900`，记录从访问到第一根 K 线的肉眼时间：**期望 8-12s**。

Ctrl+C 结束服务。

- [ ] **Step 11.3: 第二次启动（disk cache 命中 + 浏览器命中）**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/app.py nobrowser
```

`[precompress]` 日志这次应该 `压缩=0 跳过=N 耗时=<0.1s`（mtime 一致全跳过）。

浏览器访问（不要清 cache），F12 Network 看：
- charting_library 静态资源 status 是 `200 (from disk cache)` 或 `304`
- 大头资源 `library.668013...js` 不再走网络（disk cache 命中），耗时 <10ms

记录从访问到第一根 K 线的时间：**期望 2-5s**。

记录 web server 日志中第一次 `[tv_history] >>>` 出现的相对时间。

如果你切回上一个标的+周期，K 线 first=true 应该 elapsed=3ms（chart_data RAM 命中）。

- [ ] **Step 11.4: 把验证结果写到 plan 末尾留档**

在本 plan 文件末尾追加 "## Validation Results" 段落，记录两次启动的时间戳和肉眼时间，方便日后回看。

- [ ] **Step 11.5: 最终回归检查清单**

人工核对一遍：
- [ ] 至少切 3 个不同市场（a/hk/us）的标的，各无报错
- [ ] 切周期（1m / 5m / 30m / 日）无错
- [ ] 缩放、十字线、画线工具可用
- [ ] 自选股面板有数据（F2 推迟没有把它推没）
- [ ] AI 分析面板（如启用）正常
- [ ] 主题切换（dark/light）loading_screen 颜色匹配
- [ ] 重启服务后再访问，浏览器 Network 看 charting_library 资源是 disk cache 命中

---

## Self-Review

完成所有任务后，做一次最终自检：

- 每个 spec §4 改动（B1-B5、F1-F2、R1）都对应到了 Task 1-10：
  - B1 → Task 2
  - B2 → Task 1 + Task 3 钩入
  - B3 → Task 3
  - B4 → Task 4
  - B5 → Task 5+6+7
  - F1 → Task 8
  - F2 → Task 9
  - R1 → Task 10
- 没有 TBD / "implement later" / placeholder
- 类型一致：`record_user_request(market, code, frequency)` 在 Task 5 定义，Task 6 调用签名一致；`_warm_chart_cache_from_disk` 在 Task 7 定义，main() 调用一致

---

## 提交清单（最终汇总）

实施完成后的预期 commit 序列（按任务顺序）：

1. `perf(static): 添加启动期预压缩工具 (B2)` — Task 1
2. `perf(static): 添加 CachedStaticFileHandler (B1)` — Task 2
3. `perf(static): 接入预压缩 + Tornado 路由分发 (B2+B3)` — Task 3
4. `perf(startup): 默认不自动开浏览器 (B4)` — Task 4
5. `perf(startup): 添加 last_chart_state 持久化模块 (B5 part 1)` — Task 5
6. `perf(startup): tv_history 入口记录最后访问状态 (B5 part 2)` — Task 6
7. `perf(startup): 启动期把上次访问的 chart_data 预热到 RAM (B5 part 3)` — Task 7
8. `perf(frontend): TV widget loading_screen + 骨架占位 (F1)` — Task 8
9. `perf(frontend): 推迟 SymbolsPanel/ZiXuan 启动到主 widget 之后 (F2)` — Task 9
10. `chore(assets): 裁剪用不到的 i18n 包到 bundles_unused/ (R1)` — Task 10
