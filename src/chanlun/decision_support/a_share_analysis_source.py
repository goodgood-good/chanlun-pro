"""Capability-limited A-share research analysis source."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType

from chanlun.recursive_bt.select.chanlun_selector import (
    ASelectionConfig,
    OriginalChanlunASelector,
)

from .a_share_analysis_state import AResearchQuoteSource, AResearchSymbolState


@dataclass(frozen=True, slots=True)
class AResearchSourceConfig:
    """Exact fail-closed settings for the read-only research source."""

    op_level: str = "5m"
    big_level: str = "30m"
    paper_enabled: bool = False
    dingtalk_webhook: str = ""
    dry_run: bool = True
    optimization_report_enabled: bool = False
    runtime_overrides_enabled: bool = False
    warmup_new_symbols: bool = False

    def __post_init__(self) -> None:
        exact = (
            type(self.op_level) is str and self.op_level == "5m",
            type(self.big_level) is str and self.big_level == "30m",
            type(self.paper_enabled) is bool and self.paper_enabled is False,
            type(self.dingtalk_webhook) is str and self.dingtalk_webhook == "",
            type(self.dry_run) is bool and self.dry_run is True,
            type(self.optimization_report_enabled) is bool
            and self.optimization_report_enabled is False,
            type(self.runtime_overrides_enabled) is bool
            and self.runtime_overrides_enabled is False,
            type(self.warmup_new_symbols) is bool
            and self.warmup_new_symbols is False,
        )
        if not all(exact):
            raise ValueError("read-only research source settings must remain exact")


@dataclass(slots=True)
class _AResearchQuoteFacade:
    _klines: Callable[..., object] = field(repr=False)
    _stock_info: Callable[[str], object] = field(repr=False)
    kline_time_label: str

    def klines(self, code: str, frequency: str, **kwargs):
        return self._klines(code, frequency, **kwargs)

    def stock_info(self, code: str):
        return self._stock_info(code)


@dataclass(slots=True)
class _AResearchAnalysisSource:
    _universe_resolver: Callable[[], object]
    _state_factory: Callable[[str], AResearchSymbolState]
    _quote: AResearchQuoteSource
    _lock: RLock = field(default_factory=RLock)
    market: str = field(default="a", init=False)

    def resolve_universe(self, required_codes=()):
        with self._lock:
            codes, names, candidates = self._universe_resolver()
            frozen_required = _validate_required_codes(required_codes)
            base_codes = tuple(codes)
            base_set = set(base_codes)
            frozen_codes = base_codes + tuple(
                code for code in frozen_required if code not in base_set
            )
            if len(frozen_codes) != len(set(frozen_codes)):
                raise ValueError("analysis universe contains duplicate codes")
            frozen_names = _resolve_missing_names(
                frozen_codes,
                names,
                quote=self._quote,
            )
            return frozen_codes, MappingProxyType(frozen_names), tuple(candidates)

    def create_state(self, code: str):
        with self._lock:
            return self._state_factory(code)


def _validate_required_codes(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("required_codes must be an iterable of codes")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if type(value) is not str or not value or value != value.strip():
            raise ValueError("required code must be a non-empty stripped string")
        if value in seen:
            raise ValueError("required_codes contains duplicates")
        seen.add(value)
        result.append(value)
    return tuple(result)


def _resolve_missing_names(
    codes: tuple[str, ...],
    names: Mapping[str, str],
    *,
    quote: AResearchQuoteSource,
) -> dict[str, str]:
    if not isinstance(names, Mapping):
        raise TypeError("universe names must be a mapping")
    resolved: dict[str, str] = {}
    for code in codes:
        supplied = names.get(code)
        if type(supplied) is str and supplied and supplied == supplied.strip():
            resolved[code] = supplied
            continue
        info = quote.stock_info(code)
        fetched = info.get("name") if isinstance(info, Mapping) else None
        if type(fetched) is not str or not fetched or fetched != fetched.strip():
            raise RuntimeError(f"missing security name for {code}")
        resolved[code] = fetched
    return resolved


def build_a_share_selector_universe_resolver(
    selection_config: ASelectionConfig,
) -> Callable[[], tuple[tuple[str, ...], dict[str, str], tuple[object, ...]]]:
    """Build the interim research resolver from the pure A-share selector."""

    if type(selection_config) is not ASelectionConfig:
        raise TypeError("selection_config must be ASelectionConfig")

    def resolve():
        selected = OriginalChanlunASelector(selection_config).select()
        if isinstance(selected, (str, bytes)) or not isinstance(selected, Sequence):
            raise TypeError("selector.select must return a sequence")
        candidates = tuple(selected)
        codes: list[str] = []
        seen: set[str] = set()
        names: dict[str, str] = {}
        for candidate in candidates:
            code = getattr(candidate, "code", None)
            if type(code) is not str or not code or code != code.strip():
                raise ValueError("selector candidate code must be a stripped string")
            if code not in seen:
                seen.add(code)
                codes.append(code)
            name = getattr(candidate, "name", None)
            if type(name) is str and name and name == name.strip():
                existing = names.get(code)
                if existing is not None and existing != name:
                    raise ValueError("selector returned conflicting security names")
                names[code] = name
        return tuple(codes), names, candidates

    return resolve


def build_production_a_share_analysis_source(
    *,
    exchange: object,
    universe_resolver: Callable[[], object],
    config: AResearchSourceConfig = AResearchSourceConfig(),
) -> _AResearchAnalysisSource:
    """Build a source that retains only quote-read capabilities."""

    if type(config) is not AResearchSourceConfig:
        raise TypeError("config must be AResearchSourceConfig")
    config.__post_init__()
    klines = getattr(exchange, "klines", None)
    stock_info = getattr(exchange, "stock_info", None)
    if not callable(klines) or not callable(stock_info):
        raise TypeError("exchange must expose klines and stock_info")
    if not callable(universe_resolver):
        raise TypeError("universe_resolver must be callable")
    missing_label = object()
    label = getattr(exchange, "kline_time_label", missing_label)
    if label not in {"start", "end"}:
        raise ValueError("exchange kline_time_label must be start or end")
    quote = _AResearchQuoteFacade(klines, stock_info, label)
    return _AResearchAnalysisSource(
        _universe_resolver=universe_resolver,
        _state_factory=lambda code: AResearchSymbolState(code, quote),
        _quote=quote,
    )
