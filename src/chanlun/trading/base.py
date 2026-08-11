"""Minimal market-data contract used by the live screening runtime."""

from abc import ABC, abstractmethod

import pandas as pd

class MarketDatas(ABC):
    """Provide market bars and canonical Chanlun state for one data source."""

    def __init__(self, market: str, frequencys: list[str], cl_config=None) -> None:
        self.market = market
        self.frequencys = frequencys
        self.cl_config = cl_config

    @abstractmethod
    def klines(self, code, frequency) -> pd.DataFrame:
        """Return bars for a symbol and frequency."""

    @abstractmethod
    def last_k_info(self, code) -> dict:
        """Return the latest bar fields for a symbol."""
