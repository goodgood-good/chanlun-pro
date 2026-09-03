"""静态资源预压缩（B2）。

启动期一次性扫描整个 static 目录下的 .js/.css 等文本资源，
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
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name != "bundles_unused"]
        for fn in files:
            if fn.endswith(".gz"):
                continue
            if not fn.endswith(_PRECOMPRESS_EXTS):
                continue
            src = os.path.join(dirpath, fn)
            dst = src + ".gz"
            tmp_path = None
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
                tmp_path = dst + ".tmp"
                with open(src, "rb") as fi, gzip.open(
                    tmp_path, "wb", compresslevel=_GZIP_LEVEL
                ) as fo:
                    while True:
                        buf = fi.read(1024 * 256)
                        if not buf:
                            break
                        fo.write(buf)
                os.replace(tmp_path, dst)
                tmp_path = None  # 已原子替换成功，无需清理
                compressed += 1
            except Exception as e:
                LogUtil.warning(f"[precompress] {src} 失败: {e}")
                if tmp_path and os.path.isfile(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
    return (compressed, skipped, time.time() - t0)


def precompress_static_assets(static_root: str) -> None:
    """启动钩子：预压缩完整静态树，避免嵌入图重复传输入口脚本。"""
    c, s, e = precompress_directory(static_root)
    LogUtil.info(
        f"[precompress] static: 压缩={c} 跳过={s} 耗时={e:.2f}s"
    )
