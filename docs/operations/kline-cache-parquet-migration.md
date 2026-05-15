# K 线缓存 parquet 双写过渡 — 灰度与退出方案

> 关联 commit: `97257d9 perf(file_db): pickle 写盘异步化 + K 线 parquet 双写过渡 (P3/P4)`
> 关联代码: `src/chanlun/file_db.py` (US-008)
> 创建日期: 2026-05-15

## 现状（双写期）

`FileCacheDB.save_tdx_klines` 当前**同时**落两份 K 线缓存：

| 路径 | 格式 | 角色 | 何时写 |
|------|------|------|--------|
| `klines_path/{market}/{code}_{freq}.parquet` | parquet (pyarrow + zstd) | **主** | 每次 `save_tdx_klines` |
| `klines_path/{market}/{code}_{freq}.csv`     | CSV (现有) | **兜底** | 同上 |

读取路径 `get_tdx_klines`: 先尝试 parquet，失败 / 不存在时 fallback CSV。

清理路径 `clear_tdx_old_klines` 同时清理两种扩展名。

## 收益（已观察）

- 单次写盘体积：parquet ≈ CSV × **0.2-0.4**（zstd 压缩后 60-80% 减小）
- 读取速度：parquet ≈ CSV × **0.2**（pyarrow 原生 datetime 解析免 `parse_dates`）

## 退出条件（灰度满即可退）

满足以下**全部**条件时进入"只 parquet"阶段：

1. **时间窗口**：双写运行 ≥ **4 周**
2. **覆盖率**：监控 `get_tdx_klines` 走 parquet 路径的命中率 ≥ **95%**（CSV fallback < 5% 说明老数据已基本被覆盖）
3. **零回归**：4 周内无 parquet 读损坏 / unlink 异常告警（grep `LogUtil.warning` 含 `load_klines_parquet`）
4. **磁盘空间**：parquet 目录 `du -sh` 稳定增长曲线符合预期（用户实际标的数 × 单标的预估）

## 退出步骤

### 阶段 1: 停止写 CSV（只保留 parquet 写 + 双读 fallback）

```python
# src/chanlun/file_db.py:save_tdx_klines
def save_tdx_klines(self, market, code, frequency, kline):
    self.save_klines_parquet(market, code, frequency, kline)
    # 删除下面这行 CSV 写
    # csv_path = self._kline_csv_path(market, code, frequency)
    # self._atomic_write_csv(csv_path, kline)
    return True
```

部署后再观察 1 周，确认：
- 新写入只产生 .parquet 文件
- 老 CSV 仍可被 `get_tdx_klines` fallback 读取
- 无新增 fallback 失败告警

### 阶段 2: 删除 CSV 读 fallback 路径

```python
# src/chanlun/file_db.py:get_tdx_klines
def get_tdx_klines(self, market, code, frequency):
    _klines = self.load_klines_parquet(market, code, frequency)
    if _klines is None:
        return None  # 直接 miss, 不再 fallback CSV
    # ... 后续逻辑不变
```

同步：
- `_kline_csv_path` 方法可保留（其它路径可能引用）或一并删除
- `_atomic_write_csv` 如果只在此处用，可删

### 阶段 3: 清理历史 CSV 文件

`clear_tdx_old_klines` 已经同时清理两种扩展名，可在阶段 2 后跑一次完整扫描：

```bash
find {data_path}/klines -name "*.csv" -mtime +30 -delete
```

或写一次性脚本：扫描所有 `.csv`，如果同目录同前缀的 `.parquet` 已存在则删 `.csv`。

## 监控方法

### 命中率监控（阶段 1 进入前要先有这条指标）

在 `get_tdx_klines` 加 counter（也可作为后续观测点）：

```python
# 伪代码（如需正式接入指标系统）
parquet_hits, csv_hits, both_miss = Counter(), Counter(), Counter()

def get_tdx_klines(self, market, code, frequency):
    if (df := self.load_klines_parquet(market, code, frequency)) is not None:
        parquet_hits[(market, frequency)] += 1
        return df
    if (df := self._read_csv(...)) is not None:
        csv_hits[(market, frequency)] += 1
        return df
    both_miss[(market, frequency)] += 1
    return None
```

每天/每周输出一次比率（`parquet_hits / (parquet_hits + csv_hits)`）。

### 损坏率监控

`load_klines_parquet` 已有 `LogUtil.debug` 记录损坏 unlink 事件。如果 1 周内 unlink 次数 > 总写入次数 × 0.1%，**不要退出双写**，先排查 pyarrow / zstd 兼容性。

## 风险与回滚

### 风险 1: parquet 文件跨 pyarrow 版本不兼容

**症状**: 升级 pyarrow 后 `load_klines_parquet` 大量 unlink + 重写。
**缓解**: 阶段 1 部署前在 dev / staging 上跑 `pip install pyarrow=={目标版本}`，全量读一遍历史 parquet 验证无报错。

### 风险 2: zstd 压缩级别变化

`pyarrow.parquet` 不同版本 zstd 默认压缩级别可能变化，导致体积浮动。监控 `du -sh klines/` 周环比，> 50% 上涨需查 pyarrow CHANGELOG。

### 回滚（任何阶段都可执行）

回到双写：恢复 `save_tdx_klines` 的 CSV 写一行 + 恢复 `get_tdx_klines` 的 CSV fallback 即可。**parquet 历史文件不需要删除**（双写期会继续覆盖）。

### 紧急只走 CSV（极端情况）

如果 parquet 路径完全失效，临时改 `save_tdx_klines` 跳过 parquet 写、`get_tdx_klines` 跳过 parquet 读，强制走 CSV。但这等于退到 US-008 之前，仅作紧急止血。

## 何时停掉这份文档

- 阶段 3 完成后 ≥ 2 周无相关回归
- `clear_tdx_old_klines` 已实际清理掉 ≥ 99% 历史 CSV
- 没有第三方代码读取 `*.csv` 路径（grep 整仓 `_kline_csv_path` / `.csv` 文件后缀确认）

满足后把本文件移到 `docs/archive/`，标注 "已完成" 日期。

## 决策记录

- **2026-05-15**: 双写过渡上线（commit 97257d9）
- (留空, 阶段切换时填入)

---

## 附：相关代码位置

- `src/chanlun/file_db.py:367` `save_klines_parquet`
- `src/chanlun/file_db.py:398` `load_klines_parquet`
- `src/chanlun/file_db.py:416` `get_tdx_klines` (双读路径)
- `src/chanlun/file_db.py:456` `save_tdx_klines` (双写路径)
- `src/chanlun/file_db.py:483` `clear_tdx_old_klines` (双清路径)
- `tests/test_file_db_parquet.py` round-trip / fallback / 损坏自愈测试
