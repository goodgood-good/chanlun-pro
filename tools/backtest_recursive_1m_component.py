#!/usr/bin/env python3
"""Causal component replay for the user-authorized L0=1m hierarchy.

The formal lane fails closed when broker-vintage ETF execution facts are not
available.  A separate, hash-frozen diagnostic lane exercises the existing
strict completed-minute matcher under explicit assumptions; its performance
is never marked evaluable and can never enable live trading.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN
import heapq
import json
from math import sqrt
from pathlib import Path
from statistics import mean, median, stdev
import sys
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.decision_support.fingerprints import sha256_json  # noqa: E402
from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    strict_state,
)
from chanlun.decision_support.trading_system.recursive_1m_component_replay import (  # noqa: E402
    Recursive1mExecutionFactAvailability,
    Recursive1mExecutionSignal,
    Recursive1mPosition,
    diagnostic_entry_order,
    diagnostic_execution_status,
    diagnostic_exit_order,
    diagnostic_fee_model,
    size_recursive_1m_diagnostic_entry,
    tactical_reserve_for_fill,
)
from chanlun.decision_support.trading_system.recursive_1m_decision import (  # noqa: E402
    Recursive1mDataFacts,
    evaluate_recursive_1m_entry,
    evaluate_recursive_1m_exit,
)
from chanlun.decision_support.trading_system.recursive_1m_research import (  # noqa: E402
    recursive_1m_diagnostic_execution_snapshot,
    recursive_1m_parameter_manifest,
    recursive_1m_parameter_snapshot,
)
from chanlun.decision_support.trading_system.structure_adapter import (  # noqa: E402
    extract_confirmed_points,
)
from chanlun.decision_support.trading_system.v3_bar_execution import (  # noqa: E402
    HistoricalMinuteExecutionBar,
    bar_proxy_parameter_snapshot,
    match_historical_minute_bars,
)
from chanlun.decision_support.trading_system.v3_parameters import (  # noqa: E402
    etf_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v3_portfolio import (  # noqa: E402
    floor_to_increment,
)
from tools.chanlun_v3_research_data import (  # noqa: E402
    atomic_json,
    content_sha256,
    read_cached_series,
    sha256_file,
)
from tools.prescreen_recursive_1m_research import (  # noqa: E402
    DEFAULT_CORPORATE_ACTIONS,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_OUTPUT as DEFAULT_PRESCREEN,
    SPLITS,
)
from tools.prescreen_v31_cached_symbols import (  # noqa: E402
    _build_frames,
    provider_to_project_code,
)


CN = ZoneInfo("Asia/Shanghai")
INITIAL_CASH = Decimal("1000000")
DEFAULT_PIT_DATABASE = Path(
    ".cache/chanlun_v3_external_pit/etf_proxy_pit.sqlite3"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/recursive_1m_component_backtest.json"
)
DEFAULT_DATA_GATE_OUTPUT = Path(
    "audit/chanlun_live_integration/recursive_1m_data_acceptance.json"
)


def _normal(value: object) -> object:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _normal(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normal(item) for item in value]
    return value


def _verify_content_hash(payload: Mapping[str, object]) -> None:
    expected = payload.get("content_sha256")
    unsigned = dict(payload)
    unsigned.pop("content_sha256", None)
    if expected != content_sha256(unsigned):
        raise ValueError("prescreen content hash does not verify")


def _source_id(database_hash: str, provider_symbol: str, closed_at: datetime) -> str:
    return (
        f"financial-data-query:{database_hash}:{provider_symbol}:"
        f"{closed_at.isoformat()}"
    )


def _execution_bar(
    row: pd.Series,
    *,
    symbol: str,
    provider_symbol: str,
    database_hash: str,
    sequence: int,
) -> HistoricalMinuteExecutionBar:
    closed_at = row["date"].to_pydatetime()
    return HistoricalMinuteExecutionBar(
        symbol=symbol,
        opened_at=closed_at - timedelta(minutes=1),
        closed_at=closed_at,
        sequence=sequence,
        raw_open=Decimal(str(row["raw_open"])),
        raw_high=Decimal(str(row["raw_high"])),
        raw_low=Decimal(str(row["raw_low"])),
        raw_close=Decimal(str(row["raw_close"])),
        raw_volume=Decimal(str(row["volume"])),
        source_id=_source_id(database_hash, provider_symbol, closed_at),
    )


def _row_at(frame: pd.DataFrame, observed_at: datetime) -> pd.Series:
    rows = frame.loc[frame["date"] == pd.Timestamp(observed_at)]
    if len(rows) != 1:
        raise ValueError("signal availability must map to exactly one raw minute bar")
    return rows.iloc[0]


def _same_continuous_segment(left: datetime, right: datetime) -> bool:
    if left.date() != right.date():
        return False
    left_time = left.timetz().replace(tzinfo=None)
    right_time = right.timetz().replace(tzinfo=None)
    morning = time(9, 31) <= left_time < time(11, 30)
    afternoon = time(13, 1) <= left_time < time(15, 0)
    return (morning and right_time <= time(11, 30)) or (
        afternoon and right_time <= time(15, 0)
    )


def _next_execution_bar(
    frame: pd.DataFrame,
    *,
    observed_at: datetime,
    symbol: str,
    provider_symbol: str,
    database_hash: str,
) -> HistoricalMinuteExecutionBar | None:
    later = frame.loc[frame["date"] > pd.Timestamp(observed_at)]
    if later.empty:
        return None
    row = later.iloc[0]
    closed_at = row["date"].to_pydatetime()
    if not _same_continuous_segment(observed_at, closed_at):
        return None
    return _execution_bar(
        row,
        symbol=symbol,
        provider_symbol=provider_symbol,
        database_hash=database_hash,
        sequence=int(row.name),
    )


def _previous_session_close(frame: pd.DataFrame, session: date) -> Decimal | None:
    prior = frame.loc[frame["date"].dt.date < session]
    if prior.empty:
        return None
    latest_session = prior["date"].dt.date.max()
    row = prior.loc[prior["date"].dt.date == latest_session].iloc[-1]
    return Decimal(str(row["raw_close"]))


def _liquidity_cap(frame: pd.DataFrame, decision_at: datetime) -> tuple[int, dict]:
    parameters = etf_parameter_snapshot()
    prior = frame.loc[frame["date"].dt.date < decision_at.date()].copy()
    sessions = tuple(sorted(prior["date"].dt.date.unique()))
    lookback = parameters.liquidity_lookback_sessions
    selected_sessions = sessions[-lookback:]
    same_clock = decision_at.timetz().replace(tzinfo=None)
    selected = prior.loc[prior["date"].dt.date.isin(selected_sessions)]
    daily = tuple(
        Decimal(str(value))
        for value in selected.groupby(selected["date"].dt.date)["volume"].sum()
    )
    clock_rows = selected.loc[
        selected["date"].dt.time == same_clock,
        "volume",
    ]
    clock = tuple(Decimal(str(value)) for value in clock_rows)
    if len(daily) < lookback or len(clock) < lookback:
        return 0, {
            "status": "UNRESOLVED",
            "daily_sessions": len(daily),
            "same_clock_sessions": len(clock),
        }
    daily_median = median(daily)
    clock_median = median(clock)
    raw = min(
        daily_median * parameters.max_order_fraction_of_median_daily_volume,
        clock_median * parameters.max_order_fraction_of_median_same_clock_l2_volume,
    )
    cap = floor_to_increment(raw, 100)
    return cap, {
        "status": "DIAGNOSTIC_POINT_IN_TIME_COMPLETE",
        "daily_sessions": len(daily),
        "same_clock_sessions": len(clock),
        "median_daily_raw_volume": daily_median,
        "median_same_clock_1m_raw_volume": clock_median,
        "cap": cap,
    }


def _signal(
    *,
    point,
    frame: pd.DataFrame,
    provider_symbol: str,
    database_hash: str,
    parameter_set_id: str,
    kind: str,
) -> Recursive1mExecutionSignal:
    row = _row_at(frame, point.available_at)
    return Recursive1mExecutionSignal(
        signal_id=sha256_json(
            {
                "schema": "chanlun-recursive-1m-execution-signal/v1",
                "point_id": point.point_id,
                "kind": kind,
                "decision_at": point.available_at,
            }
        ),
        point_id=point.point_id,
        symbol=point.code,
        kind=kind,
        decision_at=point.available_at,
        price_basis_revision=point.price_basis_revision,
        raw_confirmation_high=Decimal(str(row["raw_high"])),
        raw_confirmation_low=Decimal(str(row["raw_low"])),
        confirmation_bar_source_id=_source_id(
            database_hash,
            provider_symbol,
            point.available_at,
        ),
        selection_snapshot_id=parameter_set_id,
    )


@dataclass(slots=True)
class _PositionState:
    public: Recursive1mPosition
    quantity: int
    original_quantity: int
    last_price: Decimal
    exit_gross: Decimal = Decimal("0")
    exit_fees: Decimal = Decimal("0")
    exit_filled_quantity: int = 0
    exit_point_id: str | None = None


def _fee_allocations(total: Decimal, fills: Sequence[object]) -> tuple[Decimal, ...]:
    if not fills:
        return ()
    notionals = tuple(
        Decimal(str(fill.quantity)) * Decimal(str(fill.execution_price))
        for fill in fills
    )
    denominator = sum(notionals, Decimal("0"))
    output: list[Decimal] = []
    used = Decimal("0")
    for notional in notionals[:-1]:
        value = (total * notional / denominator).quantize(
            Decimal("0.01"),
            rounding=ROUND_DOWN,
        )
        output.append(value)
        used += value
    output.append(total - used)
    return tuple(output)


class _DiagnosticReplay:
    def __init__(self, bundles: Mapping[str, dict], *, database_hash: str) -> None:
        self.bundles = bundles
        self.database_hash = database_hash
        self.cash = INITIAL_CASH
        self.peak_equity = INITIAL_CASH
        self.max_drawdown = Decimal("0")
        self.positions: dict[str, _PositionState] = {}
        self.pending_entries: set[str] = set()
        self.pending_exits: set[str] = set()
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.rejections: list[dict] = []
        self.closed_cycles: list[dict] = []
        self.sizing: list[dict] = []
        self.not_applicable_exit_signals = 0
        self.assumptions = recursive_1m_diagnostic_execution_snapshot()
        self.research = recursive_1m_parameter_snapshot("ETF_PROXY")
        self.strategy = etf_parameter_snapshot()
        self.fee_model = diagnostic_fee_model(self.assumptions)
        self.proxy = bar_proxy_parameter_snapshot(self.strategy)
        self._queue: list[tuple[datetime, int, int, str, object]] = []
        self._sequence = 0
        self._last_mark_at = datetime.combine(
            min(start for _name, start, _end in SPLITS),
            time(9, 30),
            tzinfo=CN,
        )

    def push(self, at: datetime, priority: int, kind: str, payload: object) -> None:
        self._sequence += 1
        heapq.heappush(
            self._queue,
            (at, priority, self._sequence, kind, payload),
        )

    def _equity(self) -> Decimal:
        return self.cash + sum(
            state.last_price * Decimal(state.quantity)
            for state in self.positions.values()
        )

    def _advance_marks(self, observed_at: datetime) -> None:
        if observed_at < self._last_mark_at:
            raise ValueError("diagnostic replay moved backwards")
        if not self.positions:
            self._last_mark_at = observed_at
            self.peak_equity = max(self.peak_equity, self.cash)
            return
        updates: dict[datetime, list[tuple[str, Decimal]]] = {}
        for symbol in self.positions:
            frame = self.bundles[symbol]["frame"]
            rows = frame.loc[
                (frame["date"] > pd.Timestamp(self._last_mark_at))
                & (frame["date"] <= pd.Timestamp(observed_at))
            ]
            for _index, row in rows.iterrows():
                at = row["date"].to_pydatetime()
                updates.setdefault(at, []).append(
                    (symbol, Decimal(str(row["raw_close"])))
                )
        for at in sorted(updates):
            for symbol, price in updates[at]:
                if symbol in self.positions:
                    self.positions[symbol].last_price = price
            equity = self._equity()
            self.peak_equity = max(self.peak_equity, equity)
            if self.peak_equity > 0:
                self.max_drawdown = max(
                    self.max_drawdown,
                    (self.peak_equity - equity) / self.peak_equity,
                )
        self._last_mark_at = observed_at

    def _account_snapshot_id(self, observed_at: datetime) -> str:
        return sha256_json(
            {
                "schema": "chanlun-recursive-1m-diagnostic-account/v1",
                "observed_at": observed_at,
                "cash": self.cash,
                "positions": {
                    symbol: {
                        "quantity": state.quantity,
                        "last_price": state.last_price,
                        "tactical_cash_reserve": (
                            state.public.tactical_cash_reserve
                        ),
                    }
                    for symbol, state in sorted(self.positions.items())
                },
            }
        )

    def _record_order(self, *, order, match, lane: str) -> None:
        self.orders.append(
            {
                "lane": lane,
                "order": asdict(order),
                "match": asdict(match),
            }
        )

    def _entry_signal(self, payload: dict) -> None:
        signal: Recursive1mExecutionSignal = payload["signal"]
        symbol = signal.symbol
        if (
            symbol in self.positions
            or symbol in self.pending_entries
            or symbol in self.pending_exits
        ):
            self.rejections.append(
                {
                    "stage": "PORTFOLIO_GATE",
                    "symbol": symbol,
                    "point_id": signal.point_id,
                    "observed_at": signal.decision_at,
                    "reason_codes": ("SYMBOL_ALREADY_OCCUPIED_OR_PENDING",),
                }
            )
            return
        bundle = self.bundles[symbol]
        frame = bundle["frame"]
        next_bar = _next_execution_bar(
            frame,
            observed_at=signal.decision_at,
            symbol=symbol,
            provider_symbol=bundle["provider_symbol"],
            database_hash=self.database_hash,
        )
        if next_bar is None:
            self.rejections.append(
                {
                    "stage": "ORDER_GATE",
                    "symbol": symbol,
                    "point_id": signal.point_id,
                    "observed_at": signal.decision_at,
                    "reason_codes": ("NO_NEXT_1M_BAR_WITHIN_CONTINUOUS_SESSION",),
                }
            )
            return
        cap, liquidity = _liquidity_cap(frame, signal.decision_at)
        equity = self._equity()
        gross = sum(
            state.last_price * Decimal(state.quantity)
            for state in self.positions.values()
        )
        protected = sum(
            state.public.tactical_cash_reserve
            for state in self.positions.values()
        )
        drawdown = (
            Decimal("0")
            if self.peak_equity <= 0
            else (self.peak_equity - equity) / self.peak_equity
        )
        sized = size_recursive_1m_diagnostic_entry(
            account_equity=equity,
            broker_cash=self.cash,
            gross_market_value=gross,
            protected_tactical_cash=protected,
            buy_limit=signal.raw_confirmation_high,
            liquidity_cap=cap,
            occupied_slots=len(self.positions),
            drawdown=drawdown,
            research=self.research,
            assumptions=self.assumptions,
            fee_session=signal.decision_at.date(),
        )
        self.sizing.append(
            {
                "symbol": symbol,
                "point_id": signal.point_id,
                "decision_at": signal.decision_at,
                "split": payload["split"],
                "liquidity": liquidity,
                "decision": asdict(sized),
            }
        )
        if sized.quantity <= 0:
            self.rejections.append(
                {
                    "stage": "SIZING_GATE",
                    "symbol": symbol,
                    "point_id": signal.point_id,
                    "observed_at": signal.decision_at,
                    "reason_codes": sized.reason_codes,
                }
            )
            return
        account_id = self._account_snapshot_id(signal.decision_at)
        order = diagnostic_entry_order(
            signal=signal,
            quantity=sized.quantity,
            next_bar=next_bar,
            account_snapshot_id=account_id,
            assumptions=self.assumptions,
            strategy=self.strategy,
        )
        previous_close = _previous_session_close(frame, signal.decision_at.date())
        if previous_close is None:
            raise ValueError("entry execution lacks a prior-session close")
        status = diagnostic_execution_status(
            known_at=signal.decision_at,
            session=signal.decision_at.date(),
            previous_close=previous_close,
            sellable_quantity=0,
            assumptions=self.assumptions,
        )
        match = match_historical_minute_bars(
            order,
            bars=(next_bar,),
            status=status,
            fee_model=self.fee_model,
            fee_session=signal.decision_at.date(),
            strategy_parameters=self.strategy,
            proxy_parameters=self.proxy,
        )
        self._record_order(order=order, match=match, lane="DIAGNOSTIC_ASSUMPTION")
        self.pending_entries.add(symbol)
        self.push(
            next_bar.closed_at,
            0,
            "ENTRY_CLOSE",
            {
                "signal": signal,
                "match": match,
                "split": payload["split"],
            },
        )

    def _entry_close(self, payload: dict) -> None:
        signal: Recursive1mExecutionSignal = payload["signal"]
        match = payload["match"]
        self.pending_entries.discard(signal.symbol)
        if not match.fills:
            self.rejections.append(
                {
                    "stage": "MATCHER",
                    "symbol": signal.symbol,
                    "point_id": signal.point_id,
                    "observed_at": signal.decision_at,
                    "reason_codes": match.rejection_and_unfilled_reasons,
                }
            )
            return
        if signal.symbol in self.positions:
            raise RuntimeError("entry fill collided with an existing position")
        quantity = match.filled_quantity
        gross = sum(
            Decimal(fill.quantity) * fill.execution_price for fill in match.fills
        )
        fees = match.total_fees
        reserve = tactical_reserve_for_fill(
            fill_notional=gross,
            fill_fee=fees,
            research=self.research,
        )
        if gross + fees + reserve > self.cash:
            raise RuntimeError("entry fill violated cash plus reserve invariant")
        self.cash -= gross + fees
        average = gross / Decimal(quantity)
        slot = min(
            set(range(1, self.strategy.slot_count + 1))
            - {state.public.slot_number for state in self.positions.values()}
        )
        opened_at = max(fill.exchange_time for fill in match.fills)
        position = Recursive1mPosition(
            cycle_id=sha256_json(
                {
                    "schema": "chanlun-recursive-1m-cycle/v1",
                    "symbol": signal.symbol,
                    "entry_point_id": signal.point_id,
                    "opened_at": opened_at,
                }
            ),
            symbol=signal.symbol,
            slot_number=slot,
            quantity=quantity,
            opened_at=opened_at,
            entry_point_id=signal.point_id,
            price_basis_revision=signal.price_basis_revision,
            average_entry_price=average,
            entry_fees=fees,
            entry_cash=gross + fees,
            tactical_cash_reserve=reserve,
        )
        self.positions[signal.symbol] = _PositionState(
            public=position,
            quantity=quantity,
            original_quantity=quantity,
            last_price=average,
        )
        allocations = _fee_allocations(fees, match.fills)
        for fill, fee in zip(match.fills, allocations, strict=True):
            self.fills.append(
                {
                    "side": "buy",
                    "symbol": signal.symbol,
                    "cycle_id": position.cycle_id,
                    "point_id": signal.point_id,
                    "split": payload["split"],
                    "exchange_time": fill.exchange_time,
                    "quantity": fill.quantity,
                    "price": fill.execution_price,
                    "fee": fee,
                    "bar_source_id": fill.bar_source_id,
                }
            )

    def _exit_signal(self, payload: dict) -> None:
        signal: Recursive1mExecutionSignal = payload["signal"]
        state = self.positions.get(signal.symbol)
        if state is None:
            self.not_applicable_exit_signals += 1
            return
        if signal.symbol in self.pending_exits:
            return
        bundle = self.bundles[signal.symbol]
        point = payload["point"]
        decision = evaluate_recursive_1m_exit(
            point=point,
            observed_at=signal.decision_at,
            parameters=self.research,
            data_facts=bundle["data_facts"],
            cycle_id=state.public.cycle_id,
            position_opened_at=state.public.opened_at,
            position_price_basis_revision=state.public.price_basis_revision,
            position_quantity=state.quantity,
        )
        if not decision.exit_eligible:
            self.rejections.append(
                {
                    "stage": "DECISION_GATE",
                    "symbol": signal.symbol,
                    "point_id": signal.point_id,
                    "observed_at": signal.decision_at,
                    "reason_codes": decision.rejected_reason_codes,
                }
            )
            return
        state.exit_point_id = signal.point_id
        remaining = state.quantity
        frame = bundle["frame"]
        session_dates = tuple(
            value
            for value in sorted(frame["date"].dt.date.unique())
            if value >= signal.decision_at.date()
        )
        reissue = 0
        scheduled = 0
        for session in session_dates:
            rows = frame.loc[frame["date"].dt.date == session]
            if session == signal.decision_at.date():
                rows = rows.loc[rows["date"] > pd.Timestamp(signal.decision_at)]
                created_at = signal.decision_at
            else:
                if rows.empty:
                    continue
                created_at = rows.iloc[0]["date"].to_pydatetime() - timedelta(
                    minutes=1
                )
            if rows.empty:
                continue
            previous_close = _previous_session_close(frame, session)
            if previous_close is None:
                continue
            position_for_order = replace(state.public, quantity=remaining)
            order = diagnostic_exit_order(
                signal=signal,
                position=position_for_order,
                created_at=created_at,
                account_snapshot_id=self._account_snapshot_id(created_at),
                sequence=reissue,
                assumptions=self.assumptions,
                strategy=self.strategy,
            )
            sellable = (
                remaining
                if session.toordinal()
                > state.public.opened_at.date().toordinal()
                else 0
            )
            status = diagnostic_execution_status(
                known_at=created_at,
                session=session,
                previous_close=previous_close,
                sellable_quantity=sellable,
                assumptions=self.assumptions,
            )
            bars = tuple(
                _execution_bar(
                    row,
                    symbol=signal.symbol,
                    provider_symbol=bundle["provider_symbol"],
                    database_hash=self.database_hash,
                    sequence=int(index),
                )
                for index, row in rows.iterrows()
            )
            match = match_historical_minute_bars(
                order,
                bars=bars,
                status=status,
                fee_model=self.fee_model,
                fee_session=session,
                strategy_parameters=self.strategy,
                proxy_parameters=self.proxy,
            )
            self._record_order(
                order=order,
                match=match,
                lane="DIAGNOSTIC_ASSUMPTION",
            )
            allocations = _fee_allocations(match.total_fees, match.fills)
            for fill, fee in zip(match.fills, allocations, strict=True):
                self.push(
                    fill.exchange_time,
                    0,
                    "EXIT_FILL",
                    {
                        "signal": signal,
                        "fill": fill,
                        "fee": fee,
                    },
                )
                scheduled += fill.quantity
            remaining -= match.filled_quantity
            reissue += 1
            if remaining == 0:
                break
        self.pending_exits.add(signal.symbol)
        if scheduled == 0:
            self.rejections.append(
                {
                    "stage": "MATCHER",
                    "symbol": signal.symbol,
                    "point_id": signal.point_id,
                    "observed_at": signal.decision_at,
                    "reason_codes": ("PERSISTENT_EXIT_UNFILLED_TO_DATA_END",),
                }
            )

    def _exit_fill(self, payload: dict) -> None:
        signal: Recursive1mExecutionSignal = payload["signal"]
        fill = payload["fill"]
        fee: Decimal = payload["fee"]
        state = self.positions.get(signal.symbol)
        if state is None or fill.quantity > state.quantity:
            raise RuntimeError("exit fill does not match current position")
        gross = Decimal(fill.quantity) * fill.execution_price
        self.cash += gross - fee
        state.quantity -= fill.quantity
        state.exit_gross += gross
        state.exit_fees += fee
        state.exit_filled_quantity += fill.quantity
        self.fills.append(
            {
                "side": "sell",
                "symbol": signal.symbol,
                "cycle_id": state.public.cycle_id,
                "point_id": signal.point_id,
                "split": next(
                    value["split"]
                    for value in self.fills
                    if value["cycle_id"] == state.public.cycle_id
                    and value["side"] == "buy"
                ),
                "exchange_time": fill.exchange_time,
                "quantity": fill.quantity,
                "price": fill.execution_price,
                "fee": fee,
                "bar_source_id": fill.bar_source_id,
            }
        )
        if state.quantity:
            return
        entry_cash = state.public.entry_cash
        pnl = state.exit_gross - state.exit_fees - entry_cash
        self.closed_cycles.append(
            {
                "cycle_id": state.public.cycle_id,
                "symbol": signal.symbol,
                "slot_number": state.public.slot_number,
                "entry_point_id": state.public.entry_point_id,
                "exit_point_id": signal.point_id,
                "opened_at": state.public.opened_at,
                "closed_at": fill.exchange_time,
                "quantity": state.original_quantity,
                "entry_cash": entry_cash,
                "exit_cash": state.exit_gross - state.exit_fees,
                "net_pnl": pnl,
                "net_return": pnl / entry_cash,
                "entry_fees": state.public.entry_fees,
                "exit_fees": state.exit_fees,
                "tactical_cash_reserved": state.public.tactical_cash_reserve,
                "tactical_cycles": 0,
            }
        )
        del self.positions[signal.symbol]
        self.pending_exits.discard(signal.symbol)

    def run(self, events: Sequence[dict]) -> dict:
        for event in events:
            self.push(
                event["signal"].decision_at,
                1 if event["kind"] == "EXIT" else 2,
                event["kind"],
                event,
            )
        while self._queue:
            observed_at, _priority, _sequence, kind, payload = heapq.heappop(
                self._queue
            )
            self._advance_marks(observed_at)
            if kind == "ENTRY":
                self._entry_signal(payload)
            elif kind == "ENTRY_CLOSE":
                self._entry_close(payload)
            elif kind == "EXIT":
                self._exit_signal(payload)
            elif kind == "EXIT_FILL":
                self._exit_fill(payload)
            else:
                raise RuntimeError(f"unknown replay event: {kind}")
            equity = self._equity()
            self.peak_equity = max(self.peak_equity, equity)
            if self.peak_equity > 0:
                self.max_drawdown = max(
                    self.max_drawdown,
                    (self.peak_equity - equity) / self.peak_equity,
                )
        end_at = datetime.combine(
            max(end for _name, _start, end in SPLITS),
            time(15, 0),
            tzinfo=CN,
        )
        self._advance_marks(end_at)
        return {
            "initial_cash": INITIAL_CASH,
            "final_cash": self.cash,
            "orders": self.orders,
            "fills": self.fills,
            "rejections": self.rejections,
            "closed_cycles": self.closed_cycles,
            "open_positions": tuple(
                {
                    **asdict(state.public),
                    "current_quantity": state.quantity,
                    "last_price": state.last_price,
                }
                for state in self.positions.values()
            ),
            "sizing_decisions": self.sizing,
            "intraday_max_drawdown": self.max_drawdown,
            "not_applicable_flat_exit_signals": self.not_applicable_exit_signals,
        }


def _daily_equity(
    *,
    database: Path,
    bundles: Mapping[str, dict],
    fills: Sequence[Mapping[str, object]],
) -> tuple[dict, ...]:
    benchmark = read_cached_series(database, symbol="000300.CSI", period="P_Day1")
    start = min(value[1] for value in SPLITS)
    end = max(value[2] for value in SPLITS)
    sessions = tuple(
        value
        for value in sorted(benchmark["source_time"].dt.date.unique())
        if start <= value <= end
    )
    ordered_fills = sorted(fills, key=lambda value: value["exchange_time"])
    fill_index = 0
    cash = INITIAL_CASH
    holdings: dict[str, int] = {}
    marks: dict[str, Decimal] = {}
    curve: list[dict] = []
    for session in sessions:
        close_at = datetime.combine(session, time(15, 0), tzinfo=CN)
        while (
            fill_index < len(ordered_fills)
            and ordered_fills[fill_index]["exchange_time"] <= close_at
        ):
            fill = ordered_fills[fill_index]
            symbol = str(fill["symbol"])
            quantity = int(fill["quantity"])
            price = Decimal(str(fill["price"]))
            fee = Decimal(str(fill["fee"]))
            if fill["side"] == "buy":
                cash -= Decimal(quantity) * price + fee
                holdings[symbol] = holdings.get(symbol, 0) + quantity
            else:
                cash += Decimal(quantity) * price - fee
                holdings[symbol] = holdings.get(symbol, 0) - quantity
                if holdings[symbol] == 0:
                    del holdings[symbol]
            fill_index += 1
        for symbol in tuple(holdings):
            frame = bundles[symbol]["frame"]
            rows = frame.loc[frame["date"].dt.date == session]
            if not rows.empty:
                marks[symbol] = Decimal(str(rows.iloc[-1]["raw_close"]))
            if symbol not in marks:
                raise RuntimeError("held symbol lacks a causal valuation mark")
        market_value = sum(
            Decimal(quantity) * marks[symbol]
            for symbol, quantity in holdings.items()
        )
        curve.append(
            {
                "session": session,
                "cash": cash,
                "market_value": market_value,
                "equity": cash + market_value,
                "occupied_symbols": len(holdings),
            }
        )
    return tuple(curve)


def _return_between(curve: Sequence[Mapping[str, object]]) -> Decimal:
    if not curve:
        return Decimal("0")
    start = Decimal(str(curve[0]["equity"]))
    end = Decimal(str(curve[-1]["equity"]))
    return end / start - Decimal("1")


def _metrics(curve: Sequence[Mapping[str, object]], result: Mapping[str, object]) -> dict:
    equities = tuple(Decimal(str(row["equity"])) for row in curve)
    net_return = equities[-1] / INITIAL_CASH - Decimal("1")
    peak = equities[0]
    maximum_drawdown = Decimal("0")
    for value in equities:
        peak = max(peak, value)
        maximum_drawdown = max(maximum_drawdown, (peak - value) / peak)
    daily_returns = tuple(
        equities[index] / equities[index - 1] - Decimal("1")
        for index in range(1, len(equities))
    )
    sharpe = None
    if len(daily_returns) >= 2 and stdev(daily_returns) > 0:
        sharpe = (
            mean(daily_returns)
            / stdev(daily_returns)
            * Decimal(str(sqrt(252)))
        )
    days = max(1, (curve[-1]["session"] - curve[0]["session"]).days)
    annualized = (Decimal("1") + net_return) ** (
        Decimal(str(365.2425 / days))
    ) - Decimal("1")
    cycles = tuple(result["closed_cycles"])
    wins = tuple(Decimal(str(row["net_pnl"])) for row in cycles if row["net_pnl"] > 0)
    losses = tuple(Decimal(str(row["net_pnl"])) for row in cycles if row["net_pnl"] < 0)
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = -sum(losses, Decimal("0"))
    notional = sum(
        Decimal(str(fill["quantity"])) * Decimal(str(fill["price"]))
        for fill in result["fills"]
    )
    fees = sum(Decimal(str(fill["fee"])) for fill in result["fills"])
    return {
        "ledger_valid": all(value > 0 for value in equities),
        "performance_evaluable": False,
        "diagnostic_assumption_metrics_present": True,
        "net_return": net_return,
        "annualized_return": annualized,
        "max_drawdown_daily": maximum_drawdown,
        "max_drawdown_intraday": result["intraday_max_drawdown"],
        "sharpe": sharpe,
        "profit_factor": (
            None if gross_loss == 0 else gross_profit / gross_loss
        ),
        "win_rate": None if not cycles else Decimal(len(wins)) / Decimal(len(cycles)),
        "payoff_ratio": (
            None
            if not wins or not losses
            else (sum(wins) / len(wins)) / (-sum(losses) / len(losses))
        ),
        "turnover": notional / (sum(equities) / Decimal(len(equities))),
        "total_fees": fees,
        "strategic_cycle_count": len(cycles),
        "tactical_cycle_count": 0,
        "order_count": len(result["orders"]),
        "fill_count": len(result["fills"]),
        "rejection_count": len(result["rejections"]),
        "strategic_sample_insufficient": len(cycles) < 100,
        "tactical_sample_insufficient": True,
        "warnings": (
            "DIAGNOSTIC_EXECUTION_ASSUMPTIONS_NOT_BROKER_VINTAGE",
            "PERFORMANCE_NOT_EVALUABLE",
            "STRATEGIC_SAMPLE_BELOW_100",
            "TACTICAL_SAMPLE_BELOW_200",
        ),
    }


def _benchmark_context(database: Path, curve: Sequence[Mapping[str, object]]) -> dict:
    benchmark = read_cached_series(database, symbol="000300.CSI", period="P_Day1")
    benchmark["session"] = benchmark["source_time"].dt.date
    output: dict[str, object] = {"symbol": "000300.CSI", "returns": {}}
    windows = (("FULL", curve[0]["session"], curve[-1]["session"]), *SPLITS)
    for name, start, end in windows:
        rows = benchmark.loc[
            (benchmark["session"] >= start) & (benchmark["session"] <= end)
        ]
        output["returns"][name] = (
            None
            if len(rows) < 2
            else Decimal(str(rows.iloc[-1]["close"]))
            / Decimal(str(rows.iloc[0]["close"]))
            - Decimal("1")
        )
    output["price_return_only"] = True
    output["not_total_return_index"] = True
    return output


def _split_and_year_tables(
    curve: Sequence[Mapping[str, object]],
    cycles: Sequence[Mapping[str, object]],
) -> tuple[dict, dict]:
    splits: dict[str, object] = {}
    for name, start, end in SPLITS:
        rows = tuple(row for row in curve if start <= row["session"] <= end)
        attributed = tuple(
            cycle
            for cycle in cycles
            if start <= cycle["opened_at"].date() <= end
        )
        splits[name] = {
            "start": start,
            "end": end,
            "diagnostic_return": _return_between(rows),
            "strategic_cycles": len(attributed),
            "net_pnl": sum(
                (Decimal(str(value["net_pnl"])) for value in attributed),
                Decimal("0"),
            ),
            "performance_evaluable": False,
        }
    years: dict[str, object] = {}
    for year in sorted({row["session"].year for row in curve}):
        rows = tuple(row for row in curve if row["session"].year == year)
        years[str(year)] = {
            "diagnostic_return": _return_between(rows),
            "performance_evaluable": False,
        }
    return splits, years


def _ablation_counts(prescreen: Mapping[str, object]) -> dict:
    decisions = tuple(
        decision
        for report in prescreen["instrument_reports"]
        for decision in report["candidate_decisions"]
    )

    def accepted_without(gates: set[str]) -> int:
        return sum(
            all(check["passed"] or check["gate"] in gates for check in row["checks"])
            for row in decisions
        )

    standard_l2_only = 0
    for row in decisions:
        checks = {value["gate"]: value for value in row["checks"]}
        l2 = checks["l2_context"]
        standard = int(str(l2["detail"]).split(";")[0].split("=")[1])
        if standard and all(value["passed"] for value in row["checks"]):
            standard_l2_only += 1
    return {
        "BASE_COMPONENT_ELIGIBLE": prescreen["totals"]["component_eligible"],
        "WITHOUT_EXPANSION_GATE": accepted_without({"expansion_state"}),
        "WITHOUT_L2_CONTEXT_GATE": accepted_without({"l2_context"}),
        "WITHOUT_NINE_SEGMENT_DERIVATION_STANDARD_L2_ONLY": standard_l2_only,
        "WITHOUT_TACTICAL": {
            "candidate_count": prescreen["totals"]["component_eligible"],
            "same_as_base": True,
            "reason": "TACTICAL_LAYER_ALREADY_UNRESOLVED_DISABLED_CASH_RESERVED",
        },
        "return_comparison_allowed": False,
    }


def _build_data_gate(
    *,
    args: argparse.Namespace,
    prescreen: Mapping[str, object],
    execution_facts: Recursive1mExecutionFactAvailability,
) -> dict:
    reports = tuple(prescreen["instrument_reports"])
    adjustment_pass = sum(
        bool(value["adjustment_gate"]["formal_chain_eligibility"])
        for value in reports
    )
    payload: dict[str, object] = {
        "schema": "chanlun-recursive-1m-data-acceptance/v1",
        "market_database": str(args.database.resolve()),
        "market_database_sha256": sha256_file(args.database),
        "pit_database": str(args.pit_database.resolve()),
        "pit_database_sha256": sha256_file(args.pit_database),
        "corporate_actions": str(args.corporate_actions.resolve()),
        "corporate_actions_sha256": sha256_file(args.corporate_actions),
        "universe_instruments": len(reports),
        "pit_adjustment_eligible_instruments": adjustment_pass,
        "requirements": (
            {
                "requirement": "1m_5m_30m_same_source",
                "status": "PASS_RESEARCH",
                "evidence": "5m/30m are aggregated only from the selected completed 1m source",
            },
            {
                "requirement": "completed_bars_only",
                "status": "PASS_RESEARCH",
                "evidence": "source start labels are normalized to completion time",
            },
            {
                "requirement": "point_in_time_adjustment",
                "status": "PARTIAL",
                "evidence": f"{adjustment_pass}/{len(reports)} ETF ledgers pass; missing ledgers reject candidates",
            },
            {
                "requirement": "historical_pool_industry_membership",
                "status": "UNRESOLVED_SURVIVOR_RISK",
                "evidence": "frozen broad-ETF artifact is not an effective-dated historical ETF master",
            },
            {
                "requirement": "suspension_ST_listing_delisting_limit_actions",
                "status": "MISSING_FOR_ETF_EXECUTION",
                "evidence": "local PIT daily status table covers stocks, not the eight ETFs",
            },
            {
                "requirement": "fundamental_market_cap_relative_value",
                "status": "NOT_APPLICABLE_ETF_PROXY; INDIVIDUAL_PATH_BLOCKED",
                "evidence": "individual point-in-time research snapshots are unavailable",
            },
            {
                "requirement": "survivor_and_missing_deletion_bias",
                "status": "UNRESOLVED",
                "evidence": "current legal-name universe may omit historically delisted peer ETFs",
            },
            {
                "requirement": "T1_fee_quantity_execution",
                "status": "PARTIAL_ENGINE_ONLY",
                "evidence": execution_facts.reason_codes,
            },
        ),
        "execution_fact_availability": asdict(execution_facts),
        "data_grade": "COMPONENT_ONLY",
        "full_system_return_evaluation_allowed": False,
        "formal_execution_return_evaluation_allowed": False,
        "diagnostic_assumption_lane_allowed": True,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    payload["content_sha256"] = content_sha256(payload)
    return payload


def build_report(args: argparse.Namespace) -> tuple[dict, dict]:
    prescreen = json.loads(args.prescreen.read_text(encoding="utf-8"))
    _verify_content_hash(prescreen)
    if prescreen.get("schema") != "chanlun-recursive-1m-etf-prescreen/v1":
        raise ValueError("unsupported recursive 1m prescreen schema")
    expected_sources = {
        "market_sha256": sha256_file(args.database),
        "pit_sha256": sha256_file(args.pit_database),
        "corporate_actions_sha256": sha256_file(args.corporate_actions),
    }
    for key, expected in expected_sources.items():
        if prescreen["source_database"][key] != expected:
            raise ValueError(f"prescreen source identity changed: {key}")
    current_manifest = recursive_1m_parameter_manifest()
    if prescreen["parameter_manifest"] != current_manifest:
        raise ValueError("prescreen parameter manifest is stale")

    eligible_reports = tuple(
        report
        for report in prescreen["instrument_reports"]
        if report["component_eligible_count"] > 0
    )
    database_hash = expected_sources["market_sha256"]
    bundles: dict[str, dict] = {}
    entry_events: list[dict] = []
    exit_events: list[dict] = []
    parity: list[dict] = []
    research = recursive_1m_parameter_snapshot("ETF_PROXY")
    for report in eligible_reports:
        provider = report["provider_symbol"]
        project = provider_to_project_code(provider)
        frames, interval, adjustment = _build_frames(
            database=args.database,
            pit_database=args.pit_database,
            corporate_actions=args.corporate_actions,
            benchmark_symbol="000300.CSI",
            provider_symbol=provider,
        )
        frame = frames["1m"]
        state = strict_state(project, "1m", frame)
        state.process_klines(frame)
        evidence = state.get_strict_evidence()
        points = extract_confirmed_points(
            evidence,
            code=project,
            source_frequency="1m",
            as_of=frame.iloc[-1]["date"],
        )
        points_by_id = {point.point_id: point for point in points}
        data_facts = Recursive1mDataFacts(
            complete_contiguous_interval=True,
            point_in_time_adjustment_complete=True,
            missing_data_inferred=False,
            source_fact_ids=(
                adjustment["effective_dated_adjustment_ledger_sha256"],
                adjustment["corporate_action_snapshot_sha256"],
                evidence.price_basis_revision,
            ),
        )
        bundles[project] = {
            "provider_symbol": provider,
            "frame": frame,
            "interval": interval,
            "data_facts": data_facts,
            "points": points,
        }
        accepted = tuple(
            value
            for value in report["candidate_decisions"]
            if value["component_eligible"]
        )
        for expected in accepted:
            point = points_by_id.get(expected["point_id"])
            if point is None:
                raise RuntimeError("final replay is missing an eligible point")
            final_decision = evaluate_recursive_1m_entry(
                point=point,
                structure=evidence.structure,
                observed_at=frame.iloc[-1]["date"].to_pydatetime(),
                parameters=research,
                data_facts=data_facts,
            )
            comparable = {
                key: expected[key]
                for key in asdict(final_decision)
            }
            final_equal = _normal(asdict(final_decision)) == comparable
            prefix = frame.loc[frame["date"] <= pd.Timestamp(point.available_at)].copy()
            prefix.attrs = frame.attrs.copy()
            prefix_state = strict_state(project, "1m", prefix)
            prefix_state.process_klines(prefix)
            prefix_evidence = prefix_state.get_strict_evidence()
            prefix_points = extract_confirmed_points(
                prefix_evidence,
                code=project,
                source_frequency="1m",
                as_of=point.available_at,
            )
            matching = tuple(
                value for value in prefix_points if value.point_id == point.point_id
            )
            if len(matching) != 1:
                raise RuntimeError("eligible point failed actual prefix reconstruction")
            prefix_decision = evaluate_recursive_1m_entry(
                point=matching[0],
                structure=prefix_evidence.structure,
                observed_at=point.available_at,
                parameters=research,
                data_facts=data_facts,
            )
            prefix_equal = _normal(asdict(prefix_decision)) == comparable
            if not final_equal or not prefix_equal:
                raise RuntimeError("live/replay or prefix decision parity failed")
            parity.append(
                {
                    "symbol": project,
                    "point_id": point.point_id,
                    "decision_at": point.available_at,
                    "final_artifact_equal": final_equal,
                    "prefix_rebuild_equal": prefix_equal,
                }
            )
            entry_events.append(
                {
                    "kind": "ENTRY",
                    "signal": _signal(
                        point=point,
                        frame=frame,
                        provider_symbol=provider,
                        database_hash=database_hash,
                        parameter_set_id=research.parameter_set_id,
                        kind="ENTRY",
                    ),
                    "point": point,
                    "split": expected["split"],
                }
            )
        earliest = min(value["signal"].decision_at for value in entry_events if value["signal"].symbol == project)
        for point in points:
            if (
                point.recursive_level == 0
                and point.point_type == "3sell"
                and point.available_at > earliest
            ):
                exit_events.append(
                    {
                        "kind": "EXIT",
                        "signal": _signal(
                            point=point,
                            frame=frame,
                            provider_symbol=provider,
                            database_hash=database_hash,
                            parameter_set_id=research.parameter_set_id,
                            kind="L0_THIRD_SELL",
                        ),
                        "point": point,
                    }
                )

    execution_facts = Recursive1mExecutionFactAvailability(
        historical_etf_trade_status=False,
        broker_vintage_fee_schedule=False,
        historical_quantity_increments=False,
        historical_settlement_rules=False,
        historical_price_limit_rules=False,
        historical_quote_or_user_waived_bar_proxy=True,
        corporate_action_ledger=True,
        source_fact_ids=(
            expected_sources["market_sha256"],
            expected_sources["pit_sha256"],
            expected_sources["corporate_actions_sha256"],
        ),
    )
    formal = {
        "intent_count": len(entry_events),
        "order_count": 0,
        "fill_count": 0,
        "performance_evaluable": False,
        "blocked_reason_codes": execution_facts.reason_codes,
        "empty_replay": True,
        "empty_return_fields_must_not_be_read_as_performance": True,
    }
    diagnostic = _DiagnosticReplay(bundles, database_hash=database_hash).run(
        (*entry_events, *exit_events)
    )
    curve = _daily_equity(
        database=args.database,
        bundles=bundles,
        fills=diagnostic["fills"],
    )
    metrics = _metrics(curve, diagnostic)
    split_table, year_table = _split_and_year_tables(
        curve,
        diagnostic["closed_cycles"],
    )
    data_gate = _build_data_gate(
        args=args,
        prescreen=prescreen,
        execution_facts=execution_facts,
    )
    report: dict[str, object] = {
        "schema": "chanlun-recursive-1m-component-backtest/v1",
        "scope": "L0_1M_STRATEGIC_STRUCTURE_COMPONENT",
        "initial_cash": INITIAL_CASH,
        "prescreen_path": str(args.prescreen.resolve()),
        "prescreen_sha256": sha256_file(args.prescreen),
        "prescreen_content_sha256": prescreen["content_sha256"],
        "parameter_manifest": current_manifest,
        "formal_execution_lane": formal,
        "diagnostic_assumption_lane": {
            **diagnostic,
            "execution_parameter_snapshot": (
                recursive_1m_diagnostic_execution_snapshot().document()
            ),
            "metrics": metrics,
            "daily_equity_curve": curve,
            "split_results": split_table,
            "year_results": year_table,
        },
        "prefix_and_decision_parity": {
            "passed": all(
                value["final_artifact_equal"] and value["prefix_rebuild_equal"]
                for value in parity
            ),
            "events": parity,
        },
        "benchmark": _benchmark_context(args.database, curve),
        "ablations": _ablation_counts(prescreen),
        "walk_forward": {
            "policy": "FROZEN_PARAMETERS_NO_REFIT",
            "windows": split_table,
            "performance_evaluable": False,
            "sample_sufficient": False,
        },
        "market_regime_results": {
            "status": "UNRESOLVED_NO_PRE_FROZEN_REGIME_LABELS",
            "year_results_provided_instead": True,
        },
        "unresolved_components": (
            "INDIVIDUAL_THREE_PROGRAM_PIT_RESEARCH_SNAPSHOTS",
            "HIGH_TIMEFRAME_DAILY_WEEKLY_MONTHLY_RISK_FACTS_FOR_RESEARCH_MAPPING",
            "LOWER_LEVEL_LOCATOR_BELOW_L0_1M",
            "TACTICAL_L1_L2_LAYER_BELOW_L0_1M",
            "FIRST_UP_LEG_FAILED",
            "SECOND_SELL_CONFIRM",
            "L0_UPMOVE_DIVERGENCE",
            *execution_facts.reason_codes,
            "HISTORICAL_EFFECTIVE_DATED_ETF_UNIVERSE_SURVIVOR_RISK",
        ),
        "data_acceptance_content_sha256": data_gate["content_sha256"],
        "data_grade": "COMPONENT_ONLY",
        "complete_system_return_claim_allowed": False,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    report["content_sha256"] = content_sha256(report)
    return report, data_gate


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--prescreen", type=Path, default=DEFAULT_PRESCREEN)
    value.add_argument("--database", type=Path, default=DEFAULT_MARKET_DATABASE)
    value.add_argument("--pit-database", type=Path, default=DEFAULT_PIT_DATABASE)
    value.add_argument(
        "--corporate-actions",
        type=Path,
        default=DEFAULT_CORPORATE_ACTIONS,
    )
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    value.add_argument(
        "--data-gate-output",
        type=Path,
        default=DEFAULT_DATA_GATE_OUTPUT,
    )
    return value


def main() -> int:
    args = parser().parse_args()
    report, data_gate = build_report(args)
    atomic_json(args.output, report)
    atomic_json(args.data_gate_output, data_gate)
    metrics = report["diagnostic_assumption_lane"]["metrics"]
    print(
        f"wrote {args.output}: cycles={metrics['strategic_cycle_count']} "
        f"performance_evaluable={metrics['performance_evaluable']}",
        flush=True,
    )
    print(f"wrote {args.data_gate_output}: COMPONENT_ONLY", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
