"""实时选股进程边界内的只读 QMT 原生工作进程。"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from multiprocessing.connection import Client, Connection
import os
from pathlib import Path
import sys
from typing import Protocol


_PROJECT_ROOT = Path(__file__).resolve().parents[4]
for _path in (_PROJECT_ROOT / "web" / "chanlun_chart", _PROJECT_ROOT / "src"):
    _value = str(_path)
    if _value not in sys.path:
        sys.path.insert(0, _value)

from cl_app.services.trading_screening_process import IPC_AUTHKEY_ENV, IPC_SCHEMA
from chanlun.decision_support.trading_system.decision_source_provenance import (
    calculate_forward_application_source_revision,
    content_addressed_source_revision_from_build,
)


class _Gateway(Protocol):
    def native_sector_assessments(self, *, as_of: object) -> object: ...

    def members(self) -> object: ...

    def changed_bars(self, since: object) -> object: ...

    def symbol_name(self, code: str) -> object: ...

    def realtime_ticks(self, codes: tuple[str, ...]) -> object: ...

    def prepare_local_history(self, **kwargs: object) -> object: ...

    def structure_bundle(self, code: str, **kwargs: object) -> object: ...

    def trading_session_evidence(self, **kwargs: object) -> object: ...


def _probe_a_share_market_data() -> dict[str, object]:
    """在工作进程宣布就绪前验证 A 股行情适配器的真实只读 RPC。"""

    from chanlun.exchange import Market, get_exchange

    exchange = get_exchange(Market.A)
    probe = getattr(exchange, "market_data_readiness_probe", None)
    if not callable(probe):
        raise RuntimeError("A 股行情适配器未实现统一就绪探针")
    raw = probe()
    if not isinstance(raw, Mapping):
        raise RuntimeError("A 股行情就绪探针返回类型无效")
    result = {str(key): value for key, value in raw.items()}
    if (
        result.get("schema") != "chanlun-qmt-market-data-readiness"
        or result.get("ready") is not True
        or result.get("provider") != "QMT_XTDATA"
        or result.get("real_account_access") is not False
        or result.get("real_order_transport") is not False
    ):
        raise RuntimeError("A 股行情就绪探针证据无效")
    return result


class _ParentDisconnected(BaseException):
    """认证父进程消失时立即终止原生工作。"""


def _qmt_fact_cache_settings(
    *,
    build_revision: str | None = None,
    data_path: Path | str | None = None,
) -> tuple[
    Path | None,
    Path | None,
    Path | None,
    str | None,
    str | None,
]:
    """只为正式启动启用规范化 QMT 事实持久化。

    手工或非正式工作进程保持缓存关闭。正式 ``app.py`` 启动会提供精确的内容寻址
    工作树版本；事实身份仍由范围收敛的 QMT 生产实现独立派生。
    """

    runtime_revision = (
        os.environ.get("CHANLUN_BUILD_REVISION", "").strip()
        if build_revision is None
        else build_revision.strip()
    )
    if content_addressed_source_revision_from_build(runtime_revision) is None:
        return None, None, None, None, None
    if data_path is None:
        from chanlun import config

        root = config.get_data_path().resolve()
    else:
        root = Path(data_path).resolve()
    from chanlun.exchange.qmt_screening_sector_source import (
        qmt_sector_composite_fact_producer_revision,
        qmt_sector_daily_fact_producer_revision,
    )

    composite_revision = qmt_sector_composite_fact_producer_revision()
    daily_revision = qmt_sector_daily_fact_producer_revision()
    support = root / "decision_support"
    return (
        support / "trading_screening_sector_frame_facts",
        support / "trading_screening_sector_daily_facts.json",
        support / "trading_screening_sector_member_status_facts",
        composite_revision,
        daily_revision,
    )


def _send_to_parent(connection: Connection, payload: Mapping[str, object]) -> None:
    """发送一帧进程通信消息；父进程消失时干净终止。

    仅重启网页时，原生调用可能尚在结束阶段，而认证套接字已经关闭。若把该重置当成
    普通远程异常，工作进程会继续向同一失效套接字发送第二帧错误并产生误导堆栈。
    父进程丢失属于进程生命周期控制，不是选股数据失败。
    """

    try:
        connection.send(dict(payload))
    except (EOFError, BrokenPipeError, OSError) as exc:
        raise _ParentDisconnected from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", required=True)
    result.add_argument("--port", required=True, type=int)
    return result


def dispatch_gateway_request(
    gateway: _Gateway,
    *,
    method: str,
    kwargs: Mapping[str, object],
) -> object:
    """严格按只读选股白名单分派请求。"""

    if method == "sector_snapshot":
        if set(kwargs) != {"as_of"}:
            raise ValueError("sector_snapshot requires exactly as_of")
        assessments = gateway.native_sector_assessments(as_of=kwargs.get("as_of"))
        members = gateway.members()
        changed_bars = gateway.changed_bars(None)
        return {
            "schema": "chanlun-native-sector-snapshot",
            "assessments": assessments,
            "members": members,
            "changed_bars": changed_bars,
            # 名称不参与排名或决策，仅在输出复核记录时延迟加载。
            "symbol_names": {},
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
    if method == "native_sector_assessments":
        return gateway.native_sector_assessments(as_of=kwargs.get("as_of"))
    if method == "members":
        if kwargs:
            raise ValueError("members accepts no arguments")
        return gateway.members()
    if method == "changed_bars":
        return gateway.changed_bars(kwargs.get("since"))
    if method == "symbol_name":
        code = kwargs.get("code")
        if not isinstance(code, str):
            raise ValueError("symbol_name requires code")
        return gateway.symbol_name(code)
    if method == "realtime_ticks":
        if set(kwargs) != {"codes"}:
            raise ValueError("realtime_ticks requires exactly codes")
        codes = kwargs.get("codes")
        if type(codes) is not tuple or any(type(code) is not str for code in codes):
            raise ValueError("realtime_ticks requires an exact string tuple")
        return gateway.realtime_ticks(codes)
    if method == "prepare_local_history":
        if set(kwargs) != {"frequency_requests", "as_of"}:
            raise ValueError(
                "prepare_local_history requires frequency_requests and as_of"
            )
        requests = kwargs.get("frequency_requests")
        if type(requests) is not tuple:
            raise ValueError("prepare_local_history requires an exact tuple")
        return gateway.prepare_local_history(
            frequency_requests=requests,
            as_of=kwargs.get("as_of"),
        )
    if method == "structure_bundle":
        code = kwargs.get("code")
        if not isinstance(code, str):
            raise ValueError("structure_bundle requires code")
        sector_members = kwargs.get("sector_members")
        if type(sector_members) is not tuple or any(
            type(value) is not str for value in sector_members
        ):
            raise ValueError("structure_bundle requires exact sector_members")
        local_history_frequencies = kwargs.get("local_history_frequencies")
        if type(local_history_frequencies) is not tuple or any(
            type(value) is not str for value in local_history_frequencies
        ):
            raise ValueError(
                "structure_bundle requires exact local_history_frequencies"
            )
        return gateway.structure_bundle(
            code,
            as_of=kwargs.get("as_of"),
            sector=kwargs.get("sector"),
            sector_members=sector_members,
            frequencies=kwargs.get("frequencies"),
            higher_timeframe_as_of=kwargs.get("higher_timeframe_as_of"),
            local_history_frequencies=local_history_frequencies,
        )
    if method == "trading_session_evidence":
        if set(kwargs) != {"session", "observed_at"}:
            raise ValueError(
                "trading_session_evidence requires session and observed_at"
            )
        return gateway.trading_session_evidence(
            session=kwargs.get("session"),
            observed_at=kwargs.get("observed_at"),
        )
    raise ValueError(f"native screening method is not allowed: {method}")


def _build_gateway(connection: Connection, request_id: list[str | None]) -> _Gateway:
    from chanlun.decision_support.trading_system.higher_timeframe_gate import (
        QmtHigherTimeframeGateSource,
    )
    from chanlun.exchange import Market, get_exchange
    from chanlun.exchange.qmt_screening_sector_source import (
        QmtSectorCompositeSource,
        QmtSectorStrengthSource,
        build_qmt_gics3_sector_catalog,
        qmt_trading_session_evidence,
        qmt_trading_sessions,
    )
    from cl_app.services.trading_screening_gateway import NativeTradingDataGateway

    def exchange_provider():
        return get_exchange(Market.A)

    def progress() -> None:
        identity = request_id[0]
        if identity is None:
            return
            # 网关有意把普通 ``Exception`` 当作单个行业的数据失败处理。
            # ``_ParentDisconnected`` 直接继承 BaseException，避免父进程死亡被误判为
            # 66 个相互独立的行业错误，而孤儿进程仍继续读取 QMT。
        _send_to_parent(
            connection,
            {
                "schema": IPC_SCHEMA,
                "type": "progress",
                "request_id": identity,
            },
        )

    (
        composite_cache,
        daily_cache,
        member_status_cache,
        composite_fact_revision,
        daily_fact_revision,
    ) = _qmt_fact_cache_settings()
    sector_frames = QmtSectorCompositeSource(
        progress_callback=progress,
        fact_cache_directory=composite_cache,
        fact_cache_revision=composite_fact_revision,
    )
    sector_strength = QmtSectorStrengthSource(
        progress_callback=progress,
        fact_cache_path=daily_cache,
        fact_cache_revision=daily_fact_revision,
        status_fact_directory=member_status_cache,
    )
    higher_timeframe = QmtHigherTimeframeGateSource(
        exchange_provider=exchange_provider,
        sector_frame_provider=sector_frames.frame,
        trading_calendar_provider=qmt_trading_sessions,
    )
    return NativeTradingDataGateway(
        exchange_provider=exchange_provider,
        sector_provider=build_qmt_gics3_sector_catalog,
        sector_frame_provider=sector_frames.frame,
        sector_strength_provider=sector_strength.strengths,
        higher_timeframe_provider=higher_timeframe.gates,
        trading_session_provider=qmt_trading_session_evidence,
        watchlist_provider=lambda: (),
        holdings_provider=lambda: (),
        progress_callback=progress,
    )


def run_worker(connection: Connection) -> int:
    request_id: list[str | None] = [None]
    application_source_revision = calculate_forward_application_source_revision(
        _PROJECT_ROOT
    )
    gateway = _build_gateway(connection, request_id)
    market_data_probe = _probe_a_share_market_data()
    _send_to_parent(
        connection,
        {
            "schema": IPC_SCHEMA,
            "type": "ready",
            "pid": os.getpid(),
            "application_source_revision": application_source_revision,
            "market_data_probe": market_data_probe,
            "real_account_access": False,
            "real_order_transport": False,
        },
    )
    while True:
        try:
            raw = connection.recv()
        except (EOFError, OSError):
            return 0
        if not isinstance(raw, Mapping) or raw.get("schema") != IPC_SCHEMA:
            return 2
        message_type = raw.get("type")
        identity = raw.get("request_id")
        if not isinstance(identity, str) or not identity:
            return 2
        if message_type == "shutdown":
            return 0
        if message_type != "request":
            return 2
        method = raw.get("method")
        kwargs = raw.get("kwargs")
        if not isinstance(method, str) or not isinstance(kwargs, Mapping):
            return 2
        request_id[0] = identity
        try:
            value = dispatch_gateway_request(
                gateway,
                method=method,
                kwargs={str(key): item for key, item in kwargs.items()},
            )
            _send_to_parent(
                connection,
                {
                    "schema": IPC_SCHEMA,
                    "type": "result",
                    "request_id": identity,
                    "value": value,
                },
            )
        except Exception as exc:
            _send_to_parent(
                connection,
                {
                    "schema": IPC_SCHEMA,
                    "type": "error",
                    "request_id": identity,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:400],
                },
            )
        finally:
            request_id[0] = None


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise ValueError("port is outside the valid range")
    try:
        authkey = bytes.fromhex(os.environ.pop(IPC_AUTHKEY_ENV, ""))
    except ValueError as exc:
        raise ValueError("authkey must be hexadecimal") from exc
    if len(authkey) != 32:
        raise ValueError("authkey must contain 32 bytes")
    connection = Client((args.host, args.port), authkey=authkey)
    try:
        try:
            return run_worker(connection)
        except _ParentDisconnected:
            return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
