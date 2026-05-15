# P8 — file_db.py 拆 4 个 Mixin 类 / 独立类 的实施方案

> 关联 commit: `674971c refactor(file_db): P8 first step — 4 职责区段标题`
> 关联代码: `src/chanlun/file_db.py`
> 创建日期: 2026-05-15

## 背景

architect 评审指出 `FileCacheDB` 759 行单类同时承担 4 种独立职责（K 线缓存 / 缠论对象缓存 / TV chart 缓存 / 通用 pkl 缓存），违反单一职责原则。P8 first step 已在文件内插入 4 个 `# P8 区段 N/4 ===` 标题划定边界。本文档给出完整 Mixin 拆分的实施方案，作为独立 PR 用 ralph 流程一次性完成。

## 拆分方案：Mixin 多继承（推荐）

### 为什么不用独立类 + composition

- **composition + delegation**：FileCacheDB 持有 4 个子对象，需要为每个公共方法写一行 forward (~30 个公共方法)。工作量大且容易遗漏。
- **__getattr__ 自动 forward**：性能开销 + IDE 静态分析失效（看不到方法）。
- **Mixin 多继承**（推荐）：物理拆分到 4 个类，FileCacheDB 多继承聚合。MRO 自动路由 self.xxx 调用。零运行时开销，IDE 仍能识别。

### 目标结构

```python
# === 共享基础设施 (保留在 FileCacheDB 主类) ===
class FileCacheDB(_KlineCacheMixin, _CLObjectCacheMixin, _GenericPklCacheMixin, _ChartDataCacheMixin):
    """统一缓存门面 (P8 Mixin 聚合)."""

    def __init__(self):
        # 所有路径 / 配置 / 锁的初始化
        ...

    # 通用 helper (供 Mixin 共用)
    def _config_md5(self, cl_config): ...
    def _make_unique_tmp_path(self, path): ...
    def _atomic_write_pickle_blocking(self, path, obj): ...
    def _atomic_write_pickle(self, path, obj): ...   # async
    def _atomic_write_csv(self, path, df): ...
    def _try_run_cleanup(self, key, fn, on_error=None): ...
```

### 4 个 Mixin 的边界

#### Mixin 1: `_KlineCacheMixin` (~130 行)

来自 file_db.py 区段 1/4。

方法列表：
- `_kline_parquet_path(market, code, frequency)`
- `_kline_csv_path(market, code, frequency)`
- `save_klines_parquet(market, code, frequency, df) -> bool`
- `load_klines_parquet(market, code, frequency) -> Optional[DataFrame]`
- `get_tdx_klines(market, code, frequency) -> Optional[DataFrame]`
- `save_tdx_klines(market, code, frequency, kline)`
- `clear_tdx_old_klines(market) -> bool`

对外部依赖（通过 self 访问，由 FileCacheDB 主类提供）：
- `self.klines_path`
- `self._make_unique_tmp_path()`
- `self._atomic_write_csv()`
- `self._try_run_cleanup()`

#### Mixin 2: `_CLObjectCacheMixin` (~260 行)

来自 file_db.py 区段 2/4。

方法列表：
- `get_web_cl_data(market, code, frequency, cl_config, klines) -> 'ICL'`（核心，160 行）
- `clear_web_cl_data(market, code)`
- `clear_old_web_cl_data()`
- `clear_all_cl_data()`
- `get_low_to_high_cl_data(market, code, frequency, cl_config, ...) -> 'ICL'`

对外部依赖：
- `self.cl_data_path`
- `self._config_md5()`
- `self._atomic_write_pickle()`
- `self.cl_data_max_age_seconds`

#### Mixin 3: `_GenericPklCacheMixin` (~25 行)

来自 file_db.py 区段 3/4。

方法列表：
- `cache_pkl_to_file(filename, data)`
- `cache_pkl_from_file(filename) -> object`

对外部依赖：
- `self.cache_pkl_path`
- `self._atomic_write_pickle()`

#### Mixin 4: `_ChartDataCacheMixin` (~70 行)

来自 file_db.py 区段 4/4。

方法列表：
- `_chart_cache_path_for(cache_key) -> Path`
- `get_chart_cache(cache_key) -> Optional[dict]`
- `set_chart_cache(cache_key, entry)`
- `delete_chart_cache(cache_key)`
- `clear_old_chart_cache()`
- `maybe_cleanup_chart_cache()`

对外部依赖：
- `self.chart_cache_path`
- `self._atomic_write_pickle()`
- `self._try_run_cleanup()`
- `self.chart_cache_max_age_seconds`
- 模块级 `_ChartCacheSafeUnpickler` （保留在文件顶部，不进 Mixin）

## 实施步骤（独立 PR 用 ralph）

### Step 1: 准备

1. 跑全量 baseline 锁定当前行为
   ```bash
   pytest tests/ -v
   python -m tests.core._record_baseline  # 刷新 4 合成 md5
   python -m script.dev.export_real_kline_fixtures  # 刷新 9 真实 md5
   ```
2. 创建分支 `refactor/p8-file-db-mixin`

### Step 2: 抽取顺序（按方法数升序，逐步验证）

1. **Mixin 3 `_GenericPklCacheMixin`**（最简单，2 方法 / 25 行）
   - 在 `class _ChartCacheSafeUnpickler` 之后插入 `class _GenericPklCacheMixin:`
   - 把 2 个方法从 FileCacheDB 内剪贴到新类（保持缩进 4 空格 → 仍 4 空格但属于新类）
   - 改 `class FileCacheDB(_GenericPklCacheMixin):`
   - 跑 baseline + 全量回归

2. **Mixin 4 `_ChartDataCacheMixin`**（6 方法 / 70 行）
   - 同样模式
   - `class FileCacheDB(_GenericPklCacheMixin, _ChartDataCacheMixin):`

3. **Mixin 1 `_KlineCacheMixin`**（7 方法 / 130 行）
   - 注意：`save_tdx_klines` 调 `self.save_klines_parquet()` 在同 Mixin 内
   - `self._atomic_write_csv()` 在 FileCacheDB 主类，通过 self 访问 OK

4. **Mixin 2 `_CLObjectCacheMixin`**（5 方法 / 260 行，最大）
   - 核心方法 `get_web_cl_data` 内部条件多，剪贴时仔细
   - 内部调用 `self._config_md5()` `self._atomic_write_pickle()` 都在 FileCacheDB 主类

### Step 3: 验证

每个 Mixin 抽完后跑：
```bash
pytest tests/ -v                                  # 全量回归
pytest tests/core/test_baseline_regression.py    # baseline 不变
python -c "from chanlun.file_db import fdb; print(type(fdb).__mro__)"  # MRO 检查
```

期望 MRO：`FileCacheDB → _KlineCacheMixin → _CLObjectCacheMixin → _GenericPklCacheMixin → _ChartDataCacheMixin → object`。

### Step 4: 进一步（可选）

如果完整 4 个 Mixin 工作良好，可以再考虑：
- 把 4 个 Mixin **拆到独立文件** `src/chanlun/file_db_mixins/{kline,cl_object,generic_pkl,chart_data}.py`
- file_db.py 仅保留 FileCacheDB 主类 + 共享 helper
- 这步是"Mixin 物理拆分到独立文件"，可作为更下游的 PR

## 风险与回滚

### 风险

- **缩进错误**：从 FileCacheDB 类体中剪贴方法到 Mixin 类时缩进必须保持 4 空格（同样在 class 内），但 IDE 自动重缩进可能多/少 4 空格。建议剪贴后 visual diff 检查。
- **遗漏方法**：4 个区段共约 24 个方法，剪贴时漏掉任一会破坏。建议每 Mixin 抽完后 `grep "def " src/chanlun/file_db.py` 对比方法数。
- **Mixin 间相互调用**：`save_tdx_klines` 内调 `self.save_klines_parquet()` 都在同一 Mixin，OK。但若 Mixin A 调 Mixin B 方法，MRO 会处理，无 functional 问题，只是不优雅。
- **下游依赖**：5+ 处 `from chanlun.file_db import fdb` 然后 `fdb.xxx()` 调用。Mixin 拆分对调用方完全透明，应无破坏。但仍需跑全量 e2e 验证。

### 回滚

任何 Mixin 步骤出问题：`git revert` 该 commit 即可（每个 Mixin 一个 atomic commit，回滚精度高）。

## 验收标准

- [ ] 4 个 Mixin 全部抽完，FileCacheDB 类体仅剩 `__init__` + 通用 helper
- [ ] `FileCacheDB.__mro__` 含 4 个 Mixin
- [ ] 全量回归 257+ passed，0 failed，0 xfailed
- [ ] P1 baseline (13 个 md5) 全部不变
- [ ] grep `from chanlun.file_db import` 任何 `fdb.xxx()` 调用仍工作
- [ ] file_db.py 单文件行数从 859 行降到 ~200 行（其余分散到 4 个 Mixin）

## 决策记录

- **2026-05-15** P8 first step: 4 区段标题划界完成（commit 674971c）
- (留空) P8 second step: GenericPklCacheMixin 抽出
- (留空) P8 third step: ChartDataCacheMixin 抽出
- (留空) P8 fourth step: KlineCacheMixin 抽出
- (留空) P8 fifth step: CLObjectCacheMixin 抽出

---

## 附：本会话不做完整 Mixin 拆分的理由

1. 本会话已累计 27 commit，继续大改动 risk 持续升高
2. file_db.py 700+ 行重排单次 Edit 难以稳妥完成
3. 完整 Mixin 拆分是纯结构改动，无 user-visible 收益（不优先于其它已完成的性能/bug 修复）
4. 单独 PR 让 review 更聚焦，每个 Mixin 抽取一个 atomic commit 易回滚

建议：本会话先 push 27 commit，独立 PR 用 ralph 流程按本文档实施。
