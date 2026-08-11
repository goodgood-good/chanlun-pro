# -*- coding: utf-8 -*-
"""Single-authority Chanlun runtime.

``CL`` builds only the production base structure (included K-lines, fractals,
strokes, segments and MACD) and the strict recursive evidence derived from it.
Centers, divergences and the three classes of buy/sell points have no mutable
side channel on ``BI``/``XD`` objects and no alternate calculator in this
runtime.
"""

from __future__ import annotations

import datetime
import threading
from functools import wraps
from typing import Any, List, Union

import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.kline_data_processor import KlineDataProcessor
from chanlun.core.macd import MACD
from chanlun.core.macd_htf import (
    CausalPartialHigherMACDCalculator,
    level_plus_one_chain,
)
from chanlun.core.strict_structure.base_profile import (
    strict_base_config,
    strict_base_config_revision,
)
from chanlun.core.strict_structure.errors import StrictStructureContractError
from chanlun.core.types import BI, CLKline, FX, ICL, Kline, XD
from chanlun.core.xd_calculator import XdCalculator


def _strict_runtime_locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._strict_evidence_lock:
            return method(self, *args, **kwargs)

    return wrapper


def _strict_contract_boundary(method):
    """Translate generic invariants only inside strict evidence construction."""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except StrictStructureContractError:
            raise
        except (ValueError, TypeError) as exc:
            raise StrictStructureContractError(str(exc)) from exc

    return wrapper


_RUNTIME_METADATA_KEYS = frozenset(
    {
        "structure_price_quantum",
        "price_basis_revision",
        "strict_base_profile_revision",
        "strict_config_revision",
    }
)


def _production_config(config: dict | None) -> dict[str, object]:
    """Bind one fixed base algorithm and accept metadata/scope fields only."""

    requested = {} if config is None else dict(config)
    base = strict_base_config()
    unknown = set(requested) - set(base) - _RUNTIME_METADATA_KEYS
    if unknown:
        raise ValueError(
            "unsupported CL configuration fields: " + ", ".join(sorted(unknown))
        )
    conflicts = {
        key: (base[key], requested[key])
        for key in set(requested) & set(base)
        if requested[key] != base[key]
    }
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise ValueError(f"production base structure configuration is fixed: {names}")
    configured_revision = requested.get("strict_base_profile_revision")
    if (
        configured_revision is not None
        and configured_revision != strict_base_config_revision()
    ):
        raise ValueError("strict base profile revision mismatch")
    result: dict[str, object] = dict(base)
    result.update(
        {key: requested[key] for key in _RUNTIME_METADATA_KEYS if key in requested}
    )
    return result


class CL(ICL):
    """Production Chanlun state with a single strict evidence authority."""

    _PICKLE_SCHEMA = "chanlun-strict-cl"
    _PICKLE_STATE_FIELDS = frozenset(
        {
            "code",
            "frequency",
            "config",
            "start_datetime",
            "market",
            "kline_processor",
            "cl_kline_processor",
            "macd_calculator",
            "bi_calculator",
            "xd_calculator",
            "_strict_htf_macd_by_level",
            "_strict_htf_macd_calculators",
            "_strict_structure_memo",
            "_strict_unit_registry",
            "_strict_price_quantum_value",
        }
    )

    def __init__(
        self,
        code: str,
        frequency: str,
        config: Union[dict, None] = None,
        start_datetime: datetime.datetime = None,
        market: str = "",
    ):
        if not isinstance(code, str) or not code:
            raise ValueError("CL code is required")
        if not isinstance(frequency, str) or not frequency:
            raise ValueError("CL frequency is required")
        if not isinstance(market, str) or not market.strip():
            raise ValueError("CL market is required")
        self.code = code
        self.frequency = frequency
        self.config = _production_config(config)
        self.start_datetime = start_datetime
        self.market = market.strip().lower()

        self.kline_processor = KlineDataProcessor(start_datetime)
        self.cl_kline_processor = CL_Kline_Process()
        self.macd_calculator = MACD()
        self.bi_calculator = BiCalculator()
        self.xd_calculator = XdCalculator()

        self._strict_htf_macd_by_level: dict[int, dict] = {}
        self._strict_htf_macd_calculators: dict[
            int, CausalPartialHigherMACDCalculator
        ] = {}
        self._strict_structure_memo: dict[object, object] = {}
        self._strict_unit_registry = None
        self._strict_price_quantum_value = None
        self._strict_evidence_lock = threading.RLock()

    def __getstate__(self):
        state = dict(self.__dict__)
        state.pop("_strict_evidence_lock", None)
        if set(state) != self._PICKLE_STATE_FIELDS:
            raise ValueError("strict CL pickle state is invalid")
        return {"_pickle_schema": self._PICKLE_SCHEMA, **state}

    def __setstate__(self, state: dict):
        if (
            not isinstance(state, dict)
            or state.get("_pickle_schema") != self._PICKLE_SCHEMA
            or set(state) != self._PICKLE_STATE_FIELDS | {"_pickle_schema"}
        ):
            raise ValueError("strict CL pickle schema is invalid")
        current = dict(state)
        current.pop("_pickle_schema")
        self.__dict__.update(current)
        self._strict_evidence_lock = threading.RLock()

    @_strict_runtime_locked
    def process_klines(self, klines: pd.DataFrame):
        src_klines = self.kline_processor.process_kline(klines)
        return self._process_src_klines(src_klines)

    @_strict_runtime_locked
    def process_kline_values(self, date, open_, high, low, close, volume=0.0):
        src_klines = self.kline_processor.process_kline_values(
            date, open_, high, low, close, volume
        )
        return self._process_src_klines(src_klines)

    def _process_src_klines(self, src_klines: List[Kline]):
        if not src_klines:
            return self
        self.macd_calculator.process_macd(self.kline_processor.klines)
        self._compute_strict_htf_macd()
        self.cl_kline_processor.process_cl_klines(self.kline_processor.klines)
        self.bi_calculator.calculate(self.cl_kline_processor.cl_klines)
        self.xd_calculator.calculate(self.bi_calculator.bis)
        self._strict_structure_memo.clear()
        return self

    def _compute_strict_htf_macd(self) -> None:
        """Update prefix-stable level-plus-one MACD evidence."""

        from chanlun.core.strict_structure.level_catalog import recursive_level_labels

        fast = int(self.config["idx_macd_fast"])
        slow = int(self.config["idx_macd_slow"])
        signal = int(self.config["idx_macd_signal"])
        max_levels = len(recursive_level_labels(self.frequency))
        targets = level_plus_one_chain(self.frequency)[:max_levels]
        results: dict[int, dict] = {}
        for level, target in enumerate(targets):
            calc = self._strict_htf_macd_calculators.get(level)
            if (
                calc is None
                or calc.frequency != self.frequency
                or calc.higher != target
                or calc.market != self.market
                or calc.fast != fast
                or calc.slow != slow
                or calc.signal != signal
            ):
                calc = CausalPartialHigherMACDCalculator(
                    self.frequency,
                    self.market,
                    fast=fast,
                    slow=slow,
                    signal=signal,
                    target_frequency=target,
                )
                self._strict_htf_macd_calculators[level] = calc
            value = calc.update(self.kline_processor.klines)
            if value is not None:
                results[level] = {**value}
        self._strict_htf_macd_by_level = results

    def get_code(self) -> str:
        return self.code

    def get_frequency(self) -> str:
        return self.frequency

    def get_config(self) -> dict:
        return dict(self.config)

    def get_src_klines(self) -> List[Kline]:
        return list(self.kline_processor.klines)

    def get_klines(self) -> List[Any]:
        return self.get_cl_klines()

    def get_cl_klines(self) -> List[CLKline]:
        return list(self.cl_kline_processor.cl_klines)

    def get_idx(self) -> dict:
        return self.macd_calculator.get_results()

    def get_fxs(self) -> List[FX]:
        return list(self.bi_calculator.fxs)

    def get_bis(self) -> List[BI]:
        return list(self.bi_calculator.bis)

    def get_xds(self) -> List[XD]:
        return list(self.xd_calculator.xds)

    def _strict_as_of(self):
        values = self.get_src_klines()
        if not values:
            raise ValueError("strict structure requires source klines")
        return values[-1].date

    def _strict_price_quantum(self):
        from decimal import Decimal, DecimalException

        raw = self.config.get("structure_price_quantum")
        if raw is None:
            raise ValueError("strict structure requires price-basis quantum metadata")
        try:
            price_quantum = Decimal(str(raw))
        except (DecimalException, ValueError) as exc:
            raise ValueError(
                "strict structure requires positive finite price quantum"
            ) from exc
        if not price_quantum.is_finite() or price_quantum <= 0:
            raise ValueError("strict structure requires positive finite price quantum")
        if self._strict_price_quantum_value is None:
            self._strict_price_quantum_value = price_quantum
        elif self._strict_price_quantum_value != price_quantum:
            raise ValueError("price quantum changed within CL lifecycle")
        return price_quantum

    def _strict_price_basis_revision(self):
        value = self.config.get("price_basis_revision")
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("strict structure requires price_basis_revision")
        return value

    def _strict_registry(self):
        from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry

        price_basis_revision = self._strict_price_basis_revision()
        if self._strict_unit_registry is None:
            self._strict_unit_registry = UnitLockRegistry(price_basis_revision)
        elif self._strict_unit_registry.price_basis_revision != price_basis_revision:
            raise ValueError("price basis changed within CL lifecycle")
        return self._strict_unit_registry

    def _validate_strict_structure_metadata(self) -> None:
        """Revalidate immutable runtime metadata before serving memoized facts."""

        self._strict_registry()
        self._strict_price_quantum()

    def _strict_config_revision(self) -> str:
        value = self.config.get("strict_config_revision")
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise ValueError("strict_config_revision must be a non-empty string")
        return value

    @_strict_runtime_locked
    def get_strict_structure_levels(self):
        from chanlun.core.strict_structure.level_catalog import recursive_level_labels
        from chanlun.core.strict_structure.models import SourceKind
        from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
        from chanlun.core.strict_structure.strength import MacdStrengthProvider
        from chanlun.core.strict_structure.unit_adapter import adapt_lines

        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("formal")
        if cached is not None:
            return cached
        price_basis_revision = self._strict_price_basis_revision()
        price_quantum = self._strict_price_quantum()
        units = adapt_lines(
            self.get_xds(),
            0,
            SourceKind.SEGMENT,
            price_quantum,
            self._strict_as_of(),
            self._strict_registry(),
        )
        labels = recursive_level_labels(self.get_frequency())
        result = StrictRecursiveEngine(max_levels=len(labels)).calculate(
            units,
            price_basis_revision=price_basis_revision,
            strength=MacdStrengthProvider(self),
        )
        self._strict_structure_memo["formal"] = result
        return result

    @_strict_runtime_locked
    def get_stroke_observation_centers(self):
        from chanlun.core.strict_structure.center_machine import calculate_centers
        from chanlun.core.strict_structure.models import SourceKind
        from chanlun.core.strict_structure.unit_adapter import adapt_lines

        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("stroke_observation")
        if cached is not None:
            return cached
        self._strict_price_basis_revision()
        units = adapt_lines(
            self.get_bis(),
            0,
            SourceKind.STROKE_OBSERVATION,
            self._strict_price_quantum(),
            self._strict_as_of(),
            self._strict_registry(),
        )
        result = calculate_centers(units, 0, SourceKind.STROKE_OBSERVATION)
        self._strict_structure_memo["stroke_observation"] = result
        return result

    def _strict_signal_engine(self):
        from chanlun.core.strict_structure.signals import StrictSignalEngine
        from chanlun.core.strict_structure.strength import MacdStrengthProvider

        return StrictSignalEngine(
            structure=self.get_strict_structure_levels(),
            strength=MacdStrengthProvider(self),
            price_quantum=self._strict_price_quantum(),
        )

    @_strict_runtime_locked
    def get_strict_points(self):
        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("confirmed_points")
        if cached is not None:
            return cached
        result = self._strict_signal_engine().confirmed_points()
        self._strict_structure_memo["confirmed_points"] = result
        return result

    @_strict_runtime_locked
    def get_strict_approaching_points(self):
        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("approaching_points")
        if cached is not None:
            return cached
        result = self._strict_signal_engine().approaching_points(self._strict_as_of())
        self._strict_structure_memo["approaching_points"] = result
        return result

    @_strict_runtime_locked
    def get_strict_divergences(self):
        from chanlun.core.strict_structure.divergence import collect_strict_divergences
        from chanlun.core.strict_structure.strength import MacdStrengthProvider

        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("divergences")
        if cached is not None:
            return cached
        result = collect_strict_divergences(
            self.get_strict_structure_levels(),
            MacdStrengthProvider(self),
        )
        self._strict_structure_memo["divergences"] = result
        return result

    @_strict_runtime_locked
    @_strict_contract_boundary
    def get_strict_evidence(self):
        from chanlun.core.strict_structure.divergence import (
            merge_formal_divergence_ledger,
        )
        from chanlun.core.strict_structure.identity import (
            build_strict_evidence_revision,
        )
        from chanlun.core.strict_structure.models import StrictEvidenceResult

        self._validate_strict_structure_metadata()
        strict_config_revision = self._strict_config_revision()
        cached = self._strict_structure_memo.get("evidence")
        if cached is not None:
            if cached.strict_config_revision != strict_config_revision:
                raise ValueError("strict config revision changed within CL lifecycle")
            return cached
        structure = self.get_strict_structure_levels()
        confirmed_points = self.get_strict_points()
        divergences = merge_formal_divergence_ledger(
            structure,
            confirmed_points,
            self.get_strict_divergences(),
        )
        price_basis_revision = self._strict_price_basis_revision()
        structure_revision = build_strict_evidence_revision(
            symbol=self.get_code(),
            source_frequency=self.get_frequency(),
            price_basis_revision=price_basis_revision,
            strict_config_revision=strict_config_revision,
            structure=structure,
            confirmed_points=confirmed_points,
            divergences=divergences,
        )
        result = StrictEvidenceResult(
            symbol=self.get_code(),
            source_frequency=self.get_frequency(),
            source_closed_at=self._strict_as_of(),
            price_basis_revision=price_basis_revision,
            structure_price_quantum=self._strict_price_quantum(),
            strict_config_revision=strict_config_revision,
            structure_revision=structure_revision,
            structure=structure,
            stroke_center_observations=self.get_stroke_observation_centers(),
            confirmed_points=confirmed_points,
            approaching_points=self.get_strict_approaching_points(),
            divergences=divergences,
        )
        self._strict_structure_memo["evidence"] = result
        return result


__all__ = ("CL",)
