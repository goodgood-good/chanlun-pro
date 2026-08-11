#!/usr/bin/env python3
"""Audit multi-prefix warmup convergence from already-local QMT K-lines.

The command is deliberately diagnostic-only.  It reads market bars with
``skip_download=True``, never reads an account, never routes an order, and
does not feed its classifications back into the active screening gate.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Protocol

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for SOURCE_ROOT in (PROJECT_ROOT / "src", PROJECT_ROOT / "web" / "chanlun_chart"):
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.decision_support.trading_system.candidate_warmup_diagnostics import (  # noqa: E402
    DEFAULT_BAR_BUDGETS,
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_FREQUENCIES,
    DEFAULT_MINIMUM_PREFIX_BARS,
    build_candidate_warmup_diagnostic_document,
    candidate_warmup_parameter_document,
    select_candidate_warmup_rows,
    unwrap_live_screening_snapshot,
)
from chanlun.decision_support.trading_system.warmup_convergence import (  # noqa: E402
    WarmupConvergenceEnvelope,
)
from chanlun.exchange.exchange import convert_stock_kline_frequency  # noqa: E402
from cl_app.services.trading_screening_gateway import (  # noqa: E402
    _closed_frame,
    _market_datetime,
    audit_native_frame_warmup_envelope,
)
from tools.research_data import atomic_json  # noqa: E402


DEFAULT_SNAPSHOT = Path(
    r"D:\chanlun_pro\decision_support\trading_screening_snapshot.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/qmt_warmup_convergence_envelope.json"
)
MINIMUM_PREFIX_BARS = DEFAULT_MINIMUM_PREFIX_BARS
_A_STOCK_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")


class FrameProvider(Protocol):
    def __call__(
        self,
        *,
        code: str,
        frequency: str,
        as_of: datetime,
        bar_budget: int,
    ) -> pd.DataFrame: ...


WarmupAuditor = Callable[..., WarmupConvergenceEnvelope]


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def _codes(value: str) -> tuple[str, ...]:
    result = tuple(item.strip().upper() for item in value.split(",") if item.strip())
    if not result or len(result) != len(set(result)):
        raise argparse.ArgumentTypeError("codes must be non-empty and unique")
    if any(_A_STOCK_CODE.fullmatch(item) is None for item in result):
        raise argparse.ArgumentTypeError("codes must be normalized A-share symbols")
    return result


def _frequencies(value: str) -> tuple[str, ...]:
    result = tuple(item.strip() for item in value.split(",") if item.strip())
    if not result or len(result) != len(set(result)):
        raise argparse.ArgumentTypeError("frequencies must be non-empty and unique")
    if any(item not in DEFAULT_FREQUENCIES for item in result):
        raise argparse.ArgumentTypeError("unsupported diagnostic frequency")
    return result


def qmt_local_frame_provider(exchange: object) -> FrameProvider:
    loader = getattr(exchange, "klines", None)
    if not callable(loader):
        raise TypeError("QMT exchange must expose klines")

    def load(
        *,
        code: str,
        frequency: str,
        as_of: datetime,
        bar_budget: int,
    ) -> pd.DataFrame:
        source_frequency = "5m" if frequency == "30m" else frequency
        requested = bar_budget * 6 if frequency == "30m" else bar_budget
        raw = loader(
            code,
            source_frequency,
            args={
                "req_counts": requested,
                "skip_download": True,
                "dividend_type": "front",
            },
        )
        source_minimum = (
            MINIMUM_PREFIX_BARS[frequency] * 6
            if frequency == "30m"
            else MINIMUM_PREFIX_BARS[frequency]
        )
        closed = _closed_frame(
            raw,
            not_after=as_of,
            minimum_bars=source_minimum,
        )
        if frequency != "30m":
            return closed
        closed.insert(0, "code", code)
        source_attrs = dict(closed.attrs)
        rebuilt = convert_stock_kline_frequency(closed, "30m")
        rebuilt.attrs = source_attrs
        return _closed_frame(
            rebuilt,
            not_after=as_of,
            minimum_bars=MINIMUM_PREFIX_BARS[frequency],
        )

    return load


def collect_qmt_warmup_convergence(
    *,
    codes: tuple[str, ...],
    frequencies: tuple[str, ...],
    as_of: datetime,
    snapshot_content_sha256: str | None,
    snapshot_wrapper_content_sha256: str | None = None,
    selected_candidates: Sequence[Mapping[str, object]] | None = None,
    parameter_document: Mapping[str, object] | None = None,
    frame_provider: FrameProvider,
    auditor: WarmupAuditor = audit_native_frame_warmup_envelope,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    for code in codes:
        for frequency in frequencies:
            try:
                frame = frame_provider(
                    code=code,
                    frequency=frequency,
                    as_of=as_of,
                    bar_budget=DEFAULT_BAR_BUDGETS[frequency],
                )
                market_data_as_of = _market_datetime(
                    frame["date"].iloc[-1],
                    "diagnostic market data close",
                )
                envelope = auditor(
                    code=code,
                    frequency=frequency,
                    frame=frame,
                    as_of=market_data_as_of,
                )
                rows.append(
                    {
                        "code": code,
                        "frequency": frequency,
                        "source": (
                            "qmt_local_completed_5m_resampled_30m"
                            if frequency == "30m"
                            else "qmt_local_completed_kline"
                        ),
                        "available_bar_count": len(frame),
                        "market_data_as_of": market_data_as_of.isoformat(),
                        "envelope": envelope.document(),
                        "semantic_diagnostic": (
                            None
                            if envelope.diagnostic is None
                            else envelope.diagnostic.document()
                        ),
                        "mapping_supply_diagnostic": (
                            None
                            if envelope.mapping_supply_diagnostic is None
                            else envelope.mapping_supply_diagnostic.document()
                        ),
                        "structure_lineage_diagnostic": (
                            None
                            if envelope.structure_lineage_diagnostic is None
                            else envelope.structure_lineage_diagnostic.document()
                        ),
                    }
                )
            except Exception as exc:
                errors.append(
                    {
                        "code": code,
                        "frequency": frequency,
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:240],
                    }
                )
    parameters = dict(
        parameter_document
        or candidate_warmup_parameter_document(
            candidate_limit=max(len(codes), 1),
            frequencies=frequencies,
        )
    )
    candidates = tuple(
        selected_candidates
        or (
            {
                "rank": index,
                "code": code,
                "source_position": index - 1,
                "lifecycle_stage": None,
                "sector_horizontal_rank": None,
                "point_type": None,
                "selection_profile": "EXPLICIT_CODES",
            }
            for index, code in enumerate(codes, start=1)
        )
    )
    if snapshot_content_sha256 is None:
        raise ValueError("candidate diagnostic requires a source snapshot identity")
    return build_candidate_warmup_diagnostic_document(
        source_content_sha256=snapshot_content_sha256,
        source_wrapper_content_sha256=snapshot_wrapper_content_sha256,
        requested_as_of=as_of,
        selected_candidates=candidates,
        rows=rows,
        errors=errors,
        parameter_document=parameters,
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    value.add_argument("--codes", type=_codes)
    value.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_CANDIDATE_LIMIT,
    )
    value.add_argument(
        "--frequencies",
        type=_frequencies,
        default=DEFAULT_FREQUENCIES,
    )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    document = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("screening snapshot must be a mapping")
    snapshot, source_identity, wrapper_identity = unwrap_live_screening_snapshot(
        document
    )
    selected_rows = select_candidate_warmup_rows(
        snapshot,
        limit=args.limit,
        explicit_codes=args.codes,
    )
    selected = tuple(str(value["code"]) for value in selected_rows)
    raw_as_of = snapshot.get("market_data_as_of") or snapshot.get("as_of")
    if not isinstance(raw_as_of, str):
        raise ValueError("screening snapshot does not expose market_data_as_of")
    as_of = datetime.fromisoformat(raw_as_of)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("screening snapshot as_of must be timezone-aware")

    from chanlun.exchange import Market, get_exchange

    parameter_document = candidate_warmup_parameter_document(
        candidate_limit=args.limit,
        frequencies=args.frequencies,
    )
    report = collect_qmt_warmup_convergence(
        codes=selected,
        frequencies=args.frequencies,
        as_of=as_of,
        snapshot_content_sha256=source_identity,
        snapshot_wrapper_content_sha256=wrapper_identity,
        selected_candidates=selected_rows,
        parameter_document=parameter_document,
        frame_provider=qmt_local_frame_provider(get_exchange(Market.A)),
    )
    atomic_json(args.output, report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "status": report["status"],
                "codes": len(selected),
                "frequencies": args.frequencies,
                "classification_counts": report["classification_counts"],
                "error_count": len(report["errors"]),
                "content_sha256": report["content_sha256"],
                "diagnostic_only": True,
                "active_gate_unchanged": True,
                "live_status": "LIVE_DISABLED",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not report["errors"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
