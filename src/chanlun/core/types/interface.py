# -*- coding: utf-8 -*-
"""Interface for the single production Chanlun runtime."""

from __future__ import annotations

import datetime
from abc import ABCMeta, abstractmethod
from typing import Any, List, Union

import pandas as pd

from chanlun.core.types.kline import CLKline, FX, Kline
from chanlun.core.types.line import BI, XD


class ICL(metaclass=ABCMeta):
    @abstractmethod
    def __init__(
        self,
        code: str,
        frequency: str,
        config: Union[dict, None] = None,
        start_datetime: datetime.datetime = None,
        market: Union[str, None] = None,
    ):
        pass

    @abstractmethod
    def process_klines(self, klines: pd.DataFrame):
        pass

    @abstractmethod
    def process_kline_values(self, date, open_, high, low, close, volume=0.0):
        pass

    @abstractmethod
    def get_code(self) -> str:
        pass

    @abstractmethod
    def get_frequency(self) -> str:
        pass

    @abstractmethod
    def get_config(self) -> dict:
        pass

    @abstractmethod
    def get_src_klines(self) -> List[Kline]:
        pass

    @abstractmethod
    def get_klines(self) -> List[Any]:
        pass

    @abstractmethod
    def get_cl_klines(self) -> List[CLKline]:
        pass

    @abstractmethod
    def get_idx(self) -> dict:
        pass

    @abstractmethod
    def get_fxs(self) -> List[FX]:
        pass

    @abstractmethod
    def get_bis(self) -> List[BI]:
        pass

    @abstractmethod
    def get_xds(self) -> List[XD]:
        pass

    @abstractmethod
    def get_strict_evidence(self):
        pass


__all__ = ("ICL",)
