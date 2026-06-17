"""独立诊断长桥(longbridge)美股行情连接。

用途：web 服务出现「美股数据拿不到 + QuoteContext 反复 rebuilt」时，
停掉生产 web 进程后单独跑本脚本，直接连一次长桥、拉美股 quote / trading_session，
看真实错误与耗时——区分两类根因：
  A) 重建风暴导致连接累积  → 重启 + 节流(_REBUILD_MIN_INTERVAL)应已缓解；
  B) 长桥服务端/网络/账户持续问题 → 重建救不了，本脚本会暴露真实错误码。

⚠️ 必须先停掉正在跑的 web 进程再跑，避免与生产抢同账户连接、加重问题。
⚠️ 需要 LONGBRIDGE_*(或 LONGPORT_*) 环境变量在当前 shell 可用
   （若你在 PyCharm run config 里配的，请在同环境下跑）。
⚠️ 若长桥真的 hang，每个 quote() 可能卡到 SDK 内部 REQUEST_TIMEOUT(~30s) 才报错，属正常。

跑法：
    C:/Users/lc/miniconda3/envs/chanlun-pro/python.exe tools/diag_lb_us.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from longbridge.openapi import QuoteContext  # noqa: E402
from chanlun.exchange.exchange_cq import _build_longbridge_config  # noqa: E402


def _check_env() -> None:
    new = ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN")
    legacy = ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")
    if not (all(os.getenv(k) for k in new) or all(os.getenv(k) for k in legacy)):
        print("⚠️  未检测到 LONGBRIDGE_*/LONGPORT_* 环境变量，config 可能构造失败。")


def main() -> None:
    _check_env()

    t0 = time.time()
    config = _build_longbridge_config()
    print(f"[1] config built in {(time.time() - t0) * 1000:.0f}ms")

    t0 = time.time()
    ctx = QuoteContext(config)
    print(f"[2] QuoteContext built in {(time.time() - t0) * 1000:.0f}ms")

    for sym in ("TSLA.US", "AAPL.US", "QQQ.US"):
        t0 = time.time()
        try:
            q = ctx.quote([sym])
            dt = (time.time() - t0) * 1000
            last = getattr(q[0], "last_done", "?") if q else "(empty)"
            print(f"[3] quote({sym}) OK in {dt:.0f}ms  last_done={last}")
        except Exception as e:
            dt = (time.time() - t0) * 1000
            print(f"[3] quote({sym}) FAILED in {dt:.0f}ms  {type(e).__name__}: {e!r}")

    t0 = time.time()
    try:
        ctx.trading_session()
        print(f"[4] trading_session OK in {(time.time() - t0) * 1000:.0f}ms")
    except Exception as e:
        dt = (time.time() - t0) * 1000
        print(f"[4] trading_session FAILED in {dt:.0f}ms  {type(e).__name__}: {e!r}")

    print("\n解读：")
    print("  - 全部 OK 且快(<1s)      → 长桥本身正常，之前是重建风暴/连接累积，重启+节流应已缓解(根因A)")
    print("  - quote 卡很久才报错/超时 → 长桥美股链路持续问题，重建救不了(根因B：网络/账户/服务端)")
    print("  - OpenApiException(301xxx) → 看 code：配额/权限/限流，需登录长桥后台核对账户")


if __name__ == "__main__":
    main()
