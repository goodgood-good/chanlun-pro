# -*- coding: utf-8 -*-
"""
技术指标计算模块 - MACD 高精度修正版

支持增量更新（Tick 级更新最后一根、Bar 级新增 K 线），避免每次全量重算。
"""
from typing import List, Dict, Tuple
import pandas as pd
from chanlun.core.cl_interface import Kline


class MACD:
    """
    MACD指标计算类（支持增量更新）
    """

    def __init__(self, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9, china_mode: bool = True):
        """
        Args:
            fast_period: 快线周期
            slow_period: 慢线周期
            signal_period: 信号线周期
            china_mode: 是否使用国内软件标准 (默认 True, (DIF-DEA)*2)
        """
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.china_mode = china_mode

        # EMA 增量计算的衰减系数
        self.fast_alpha = 2 / (self.fast_period + 1)
        self.slow_alpha = 2 / (self.slow_period + 1)
        self.signal_alpha = 2 / (self.signal_period + 1)

        self.dif: List[float] = []
        self.dea: List[float] = []
        self.hist: List[float] = []
        self.hist_area: List[float] = []

        # _val 为当前最后一根 K 线 EMA 值(N)，_prev 为倒数第二根(N-1)；
        # Tick 更新最后一根时需要回到 N-1 重算 N。
        self._ema_fast_val: float = 0.0
        self._ema_fast_val_prev: float = 0.0

        self._ema_slow_val: float = 0.0
        self._ema_slow_val_prev: float = 0.0

        self._dea_val: float = 0.0
        self._dea_val_prev: float = 0.0

        # 记录上次处理的数据长度，用于判断是"新增"还是"更新"
        self._last_kline_count = 0

    def process_macd(self, klines: List[Kline]):
        """
        执行MACD计算 (支持增量)
        """
        current_count = len(klines)

        if current_count == 0:
            self._reset_data()
            return

        if self._last_kline_count == 0 or current_count < self._last_kline_count:
            # 模式A：全量计算（初始化或历史数据变动）
            self._full_calculation(klines)

        elif current_count == self._last_kline_count:
            # 模式B：Tick 更新最后一根，先回滚到倒数第二根再重算
            self._rollback_state()
            self._incremental_calculation(klines[-1], is_update_last=True)

        elif current_count > self._last_kline_count:
            # 模式C：新增 Bar 追加计算
            new_klines = klines[self._last_kline_count:]
            for k in new_klines:
                # 计算新 Bar 前，当前的 val 变成 prev
                self._ema_fast_val_prev = self._ema_fast_val
                self._ema_slow_val_prev = self._ema_slow_val
                self._dea_val_prev = self._dea_val

                self._incremental_calculation(k, is_update_last=False)

        self._last_kline_count = current_count

    def _full_calculation(self, klines: List[Kline]):
        """全量 Pandas 计算 (初始化用)"""
        close_prices = pd.Series([k.c for k in klines], dtype='float64')

        ema_fast = close_prices.ewm(span=self.fast_period, adjust=False).mean()
        ema_slow = close_prices.ewm(span=self.slow_period, adjust=False).mean()

        dif_series = ema_fast - ema_slow
        dea_series = dif_series.ewm(span=self.signal_period, adjust=False).mean()

        self.dif = dif_series.fillna(0).tolist()
        self.dea = dea_series.fillna(0).tolist()

        hist_series = pd.Series(self.dif) - pd.Series(self.dea)

        if self.china_mode:
            hist_series *= 2

        self.hist = hist_series.fillna(0).tolist()

        # 缓存 EMA 状态，供后续增量计算续接
        if len(klines) > 0:
            self._ema_fast_val = ema_fast.iloc[-1]
            self._ema_slow_val = ema_slow.iloc[-1]
            self._dea_val = dea_series.iloc[-1]

            if len(klines) > 1:
                self._ema_fast_val_prev = ema_fast.iloc[-2]
                self._ema_slow_val_prev = ema_slow.iloc[-2]
                self._dea_val_prev = dea_series.iloc[-2]
            else:
                # 只有 1 根 K 线时无前值，prev 设为同值；
                # 第 1 根的 Tick 更新本就走重算，不依赖 prev。
                self._ema_fast_val_prev = self._ema_fast_val
                self._ema_slow_val_prev = self._ema_slow_val
                self._dea_val_prev = self._dea_val

        self.hist_area = []
        self._calculate_hist_area_incremental(self.hist, start_index=0)

    def _incremental_calculation(self, kline: Kline, is_update_last: bool = False):
        """增量单根计算 (手动实现 EMA 公式)"""
        close = kline.c

        # 基准统一用 _prev：Tick 更新基准是 N-1；新增 Bar 时 process_macd
        # 循环已把 N 滚成 prev，故两种模式都以 _prev 为基准。
        base_fast = self._ema_fast_val_prev
        base_slow = self._ema_slow_val_prev
        base_dea = self._dea_val_prev

        # EMA_today = alpha * Price + (1 - alpha) * EMA_prev
        new_ema_fast = (close * self.fast_alpha) + (base_fast * (1 - self.fast_alpha))
        new_ema_slow = (close * self.slow_alpha) + (base_slow * (1 - self.slow_alpha))
        new_dif = new_ema_fast - new_ema_slow
        # DEA 是对 DIF 的 EMA
        new_dea = (new_dif * self.signal_alpha) + (base_dea * (1 - self.signal_alpha))

        new_hist = (new_dif - new_dea)
        if self.china_mode:
            new_hist *= 2

        self._ema_fast_val = new_ema_fast
        self._ema_slow_val = new_ema_slow
        self._dea_val = new_dea

        # Tick 更新模式下列表已在 _rollback_state pop 过，两种模式都用 append
        if is_update_last:
            self.dif.append(new_dif)
            self.dea.append(new_dea)
            self.hist.append(new_hist)
        else:
            self.dif.append(new_dif)
            self.dea.append(new_dea)
            self.hist.append(new_hist)

        self._calculate_hist_area_incremental(self.hist, start_index=len(self.hist) - 1)

    def _rollback_state(self):
        """当检测到是更新当前 Bar 时，移除列表最后一位，准备重新计算"""
        if self.dif:
            self.dif.pop()
            self.dea.pop()
            self.hist.pop()
            self.hist_area.pop()

    def _calculate_hist_area_incremental(self, hist_data: List[float], start_index: int):
        """
        增量计算面积
        :param start_index: 开始计算的索引。
        """
        if start_index == 0:
            current_area = 0.0
            direction = 0
            self.hist_area = [0.0] * len(hist_data)
        else:
            # 增量模式：继承前一个索引的面积与方向状态
            prev_idx = start_index - 1
            if prev_idx >= 0:
                current_area = self.hist_area[prev_idx]
                val_prev = hist_data[prev_idx]
                if val_prev > 0:
                    direction = 1
                elif val_prev < 0:
                    direction = -1
                else:
                    direction = 0
            else:
                current_area = 0.0
                direction = 0

            # 补齐数组长度
            if len(self.hist_area) < len(hist_data):
                self.hist_area.extend([0.0] * (len(hist_data) - len(self.hist_area)))

        for i in range(start_index, len(hist_data)):
            val = hist_data[i]

            if abs(val) < 1e-9:
                val = 0.0

            if val == 0:
                self.hist_area[i] = current_area
                continue

            current_dir = 1 if val > 0 else -1

            if direction == 0:
                direction = current_dir
                current_area = val
            elif current_dir == direction:
                # 同向累加
                current_area += val
            else:
                # 反向重置：新周期的开始，面积等于当前值
                direction = current_dir
                current_area = val

            self.hist_area[i] = current_area

    def _reset_data(self):
        self.dif, self.dea, self.hist, self.hist_area = [], [], [], []
        self._last_kline_count = 0
        self._ema_fast_val = 0.0
        self._ema_fast_val_prev = 0.0
        self._ema_slow_val = 0.0
        self._ema_slow_val_prev = 0.0
        self._dea_val = 0.0
        self._dea_val_prev = 0.0

    def get_results(self) -> Dict[str, Dict[str, List[float]]]:
        return {
            'macd': {
                'dif': self.dif,
                'dea': self.dea,
                'hist': self.hist,
                'hist_area': self.hist_area
            }
        }