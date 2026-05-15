# H1+H2 根治:HTF MACD 改为"真合成高周期 K 线 → 算 MACD → 投影回低周期"

**日期**:2026-05-15
**作者**:lc
**状态**:设计待执行

**关联代码**:

- `web/chanlun_chart/cl_app/services/chart_compute.py`(核心改动)
- `web/chanlun_chart/cl_app/blueprints/tv.py`(导入清理)
- `tests/test_apply_higher_macd.py`(整体重写)
- `web/chanlun_chart/scripts/verify_higher_macd.py`(新增手动验证脚本)

---

## 背景

`MACD_HTF` 指标在前端图表上(尤其 TSLA 1min)与高周期(5min)真实 MACD 走势对不上,根因有两个:

### H1 — 算法不等价"真实 5min MACD"

当前 `apply_higher_macd_to_chart_data` 在低周期 closes 上跑"放大参数"的 EMA:

```python
ratio = HIGHER_MACD_RATIO["1m"]  # = 5
fast = 12 * ratio    # = 60
slow = 26 * ratio    # = 130
signal = 9 * ratio   # = 45
talib.MACD(closes_1min, 60, 130, 45)
```

数学上 `EMA(60)` 作用于 1min 每根 close ≠ `EMA(12)` 作用于"每 5 根 1m 才采一个样本"的 5m close。两者是不同的低通滤波器,前者更平滑、拐点位移。前端用户在 1m 图看到的 HTF 红绿柱位置与 5m 真实 MACD **对不上**。

### H2 — 美股跨夜断层污染

TSLA 1min 盘前盘后无数据,closes 数组里"昨日 16:00 → 今日 09:30" 是相邻索引。`talib.MACD` 不知日界,EMA(130) 把 17.5 小时夜盘当作 130 根相邻 1m 做衰减。后果:每个交易日开盘后约 30~60 根 1m,HTF 出现伪 spike 或反向拐点,与高周期真实 MACD 不符。

---

## 目标

1. HTF MACD 数值在数学上等价"将低周期 K 线按市场时区合成高周期 K 线,再跑标准 `talib.MACD(12,26,9)`"
2. 跨夜断层、半日休市、午休不再污染 HTF MACD(由合成 bin 自然处理)
3. 演化模式:当前未收盘的高周期 bar 用 bin 内最后一根低周期 close 作"演化中 close",与现行 UX 一致
4. 覆盖所有现有周期对:`1m→5m / 5m→30m / 30m→d / d→w / w→M`
5. `pytest tests/test_apply_higher_macd.py` 通过

## 非目标

- 不改前端 study(`chart_idx_macd_backend.js`)显示逻辑;HTF 数据接口契约不变(仍是 `higher_macd_dif/dea/hist`,长度 == `chart_data["c"]`)
- 不改 `enable_kchart_low_to_high` 流程(它处理 cl_data 合成,与 HTF MACD 独立)
- 不引入 feature flag / 灰度开关 — 直接替换,`git revert` 即回滚
- 不改 currency / fx 24h 市场的"日界"语义(沿用 UTC 切日)

## 已知行为变更

- **月线(M)失去 HTF 显示**:旧实现 hardcoded `frequency == "m"` 返回 ratio=12(等价"月线 → 年线 MACD")。新 `HIGHER_FREQ_MAP` 中 `"M" -> None`,月线图将不再有 HTF 红绿柱。理由:月线看 HTF 的用户极少;年线 12 个月 EMA 信号实际意义有限;减少一个 bin 分支(年 bin 公式)的实现/测试成本。若用户反馈需要恢复,后续可扩展 `M -> "Y"`,bin 公式 `year` in market_tz。

---

## 架构

### 数据流

```
chart_data["t"(秒)] + chart_data["c"]
  │
  ├─ _resolve_higher_target_freq(frequency, market)
  │   └─ 返回 target_freq ∈ {"5m","30m","d","w","M",None}
  │
  ├─ _bin_keys_for_higher(times, target_freq, market) -> bin_keys[N_low]
  │   └─ 每根低周期 K 线归属哪个高周期 bin (int64)
  │
  ├─ _resample_closes_to_higher(closes, bin_keys)
  │   └─ 返回 (higher_closes[N_high], low_to_higher_idx[N_low])
  │       higher_closes:演化模式,bin 内 last close 覆盖
  │       low_to_higher_idx:低周期第 i 根 → 高周期第 j 根
  │
  ├─ talib.MACD(higher_closes, 12, 26, 9) -> h_dif/h_dea/h_hist (高周期长度)
  │
  └─ higher_macd_*[i] = h_*[low_to_higher_idx[i]]   # 投影回低周期长度
      写回 chart_data["higher_macd_dif/dea/hist"]
```

### 关键函数签名

```python
def _resolve_higher_target_freq(frequency: str, market: str) -> str | None:
    """返回 frequency 对应的目标高周期标识符,无对照时 None。

    映射(HIGHER_FREQ_MAP 总表):
      1m -> "5m"
      5m -> "30m"
      30m -> "d"
      d  -> "w"
      w  -> "M"
      M  -> None
    """

def _bin_keys_for_higher(
    times: np.ndarray,   # int64,秒级 epoch
    target_freq: str,    # "5m"/"30m"/"d"/"w"/"M"
    market: str,
) -> np.ndarray:         # int64,长度 = len(times)
    """每根低周期 bar 应归属的高周期 bin id。

    "5m":  epoch // 300
    "30m": epoch // 1800
    "d":   tz-aware datetime.toordinal()
    "w":   iso_year * 100 + iso_week  (周一为首)
    "M":   year * 100 + month

    market_tz 决定 d/w/M 的"日"如何切。
    """

def _resample_closes_to_higher(
    closes: np.ndarray,    # float64
    bin_keys: np.ndarray,  # int64
) -> tuple[np.ndarray, np.ndarray]:
    """演化模式合成。

    返回:
      higher_closes:每个唯一 bin 的"演化 close"(bin 内 last close)
      low_to_higher_idx:长度 = len(closes),指向 higher_closes 的索引
    """

def apply_higher_macd_to_chart_data(
    chart_data: dict, frequency: str, market: str, cl_config: dict,
) -> None:
    """重写后主入口。in-place 写 higher_macd_dif/dea/hist。

    short-circuit(保留旧契约):
      - target_freq is None        -> 不写字段
      - len(closes) == 0           -> 不写字段
      - len(higher_closes) <= slow + signal -> 不写字段
    """
```

### 市场时区表(新增到 `chart_compute.py` 顶部)

```python
MARKET_TZ = {
    "a": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
    "ny_futures": "America/New_York",
    "futures": "Asia/Shanghai",
    "currency": "UTC",
    "currency_spot": "UTC",
    "fx": "UTC",
}
# 未知 market 默认 "UTC"
```

### bin 函数语义表

| target_freq | bin_id 公式 | 跨夜处理 | 备注 |
|---|---|---|---|
| 5m | `epoch // 300` | 自然(epoch 不同 bin 不同) | 任何时区都对 |
| 30m | `epoch // 1800` | 自然 | 同上 |
| d | `(datetime in mkt_tz).toordinal()` | 需市场时区 | 美股 ET 16:00 后归属当日 |
| w | `iso_year*100 + iso_week`(mkt_tz) | 跨周日自然分组 | ISO 周(周一为首) |
| M | `year*100 + month`(mkt_tz) | 跨月自然分组 | |

---

## 删除的代码

`chart_compute.py`:

- `HIGHER_MACD_RATIO` 常量表(L50-53)
- `MARKET_30M_TO_D_RATIO`(L56-65)
- `MARKET_D_TO_W_RATIO`(L68-77)
- `_resolve_higher_macd_ratio` 函数(L423-438)
- `apply_higher_macd_to_chart_data` 内 `fast = idx_macd_fast * ratio` 等"放大参数"代码(L463-492 整段重写)

`tv.py`:

- L161-163 三个常量的 import → 删除

`tests/test_apply_higher_macd.py`:

- 整体重写(原 `test_ratio_*` 系列全删,因为底层符号被删)

新增:

- `chart_compute.py` 新增 `HIGHER_FREQ_MAP` + `MARKET_TZ` 常量,3 个内部函数,重写 `apply_higher_macd_to_chart_data`
- `scripts/verify_higher_macd.py` 验证脚本

---

## 测试策略(B+C)

### B 重写 `tests/test_apply_higher_macd.py`(必做,回归保护)

每个内部函数都有覆盖:

1. **`_resolve_higher_target_freq`**:
   - 各 (frequency, market) 组合返回正确目标
   - `"M"` / 未知 frequency 返回 `None`
2. **`_bin_keys_for_higher`**:
   - `5m`/`30m`:相邻两个 epoch 跨整数倍秒分界时 bin_id +1
   - `d` 美股:同一交易日不同 30m bar bin_id 相同;跨夜两根 bin_id 相差 1
   - `w`:周一到周五五根日线 bin_id 相同;下周一 bin_id +1
   - `M`:同月不同周日线 bin_id 相同;跨月 +1
3. **`_resample_closes_to_higher`**:
   - 演化模式:同 bin 内多根 close,`higher_closes` 取最后一根
   - bin 切换:`higher_closes` 长度增加
   - `low_to_higher_idx` 单调非降,`len(set) == len(higher_closes)`
4. **`apply_higher_macd_to_chart_data` 端到端**:
   - 短序列:字段不存在(保留 `test_apply_short_series_no_op`)
   - 足够长(500 根 1m):字段存在,长度 == `len(closes)`,头部 NaN→None
   - **Numerical equivalence**:构造 1m closes(含跨夜),新算法输出 `higher_macd_hist[i]` 必须 == 直接对手动合成的 5m closes 跑 `talib.MACD(12,26,9)` 得到的 `ref_hist[low_to_higher_idx[i]]`(误差 < 1e-9)
   - **跨夜污染验证**:对比"含 17 小时夜间断层"vs"无断层"两份 1m 数据,新算法在开盘段 HTF 一致

### C `scripts/verify_higher_macd.py`(手动验证,不进 CI)

用真实股票数据出对照报告,佐证新算法在生产数据上确实修正了 H1+H2:

```
[INPUT] TSLA 1m, last 5 trading days, ~1950 bars
[ALG-NEW] apply_higher_macd_to_chart_data (resample)
[ALG-OLD] _apply_higher_macd_scale_legacy (脚本内本地保留旧逻辑,跑完即弃)
[REF-5M]  手动合成 5m closes 跑 talib.MACD(12,26,9) 再投影回 1m

| Metric                              | NEW vs REF | OLD vs REF |
|-------------------------------------|------------|------------|
| mean(|diff|) on hist                | ~0         | 显著>0     |
| max(|diff|)                         | ~0         | 显著>0     |
| median diff on first 10 bars after  | ~0         | 显著>0     |
|   each session open (跨夜后开盘)    |            |            |
| max diff on first 10 bars after open| ~0         | 显著>0     |
```

接受标准:NEW 列所有 metric < 1e-6;OLD 列至少一个 metric 显著大于 NEW。

---

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| `_bin_keys_for_higher("d", "futures")` 国内期货夜盘(21:00→次日 02:30)归属哪日?不同合约不同 | 沿用 `cl_utils` / `kchart_low_to_high` 现行约定;不一致由 verify 脚本暴露 |
| ISO 周与项目其他地方"周"定义可能冲突 | spec 固定 ISO 周(周一为首),与中国/美股惯例一致;W bin id 不与其他模块共享 |
| `len(higher_closes)` 不足时 `talib.MACD` 报错 | 已有 `<= slow + signal` 守卫;无 silent fail |
| HTF 数值"突变"被反馈"指标变了" | commit message 明确"修正:HTF 之前用参数放大近似,现改为真合成" |
| 其他模块依赖 `HIGHER_MACD_RATIO` / `_resolve_higher_macd_ratio` | 实施前 grep 全仓二次确认;当前 grep 显示仅 `tv.py` 导入 + 测试文件引用 |
| 性能退化 | 新算法 O(N) 一次扫描 + 一次 talib;`higher_closes` 更短,talib 更快;预期净持平或略快 |

**回滚**:`git revert <commit>` 即可,无 schema / 缓存 key / 前端 API 变更。

---

## 工作量估算

- bin 函数 + resample + 重写 `apply_higher_macd_to_chart_data`:~80 行核心代码
- 重写 `tests/test_apply_higher_macd.py`:~150 行
- `scripts/verify_higher_macd.py`:~120 行
- 合计约 350 行;约 1 个工作日(含 verify 脚本跑通)
