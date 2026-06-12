import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import (
    audit_robustness_evidence,
    audit_us_selection_sources,
    prewarm_live_backtest_signal_cache,
)


class _CheckpointFakeCL:
    fail_after = None

    def __init__(self, _code, frequency, _config):
        self.frequency = frequency
        self.dates = []
        self._last_mmd_sig = None

    def process_klines(self, df):
        for date in list(df["date"]):
            if self.fail_after is not None and len(self.dates) >= self.fail_after:
                raise RuntimeError("checkpoint stop")
            self.dates.append(date)
            self._last_mmd_sig = ("rows", len(self.dates))


def _json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_us_selection_source_audit_keeps_missing_fundamentals_as_gap(tmp_path):
    cache = tmp_path / "chart_cache"
    for freq in ("1m", "5m", "30m"):
        (cache / f"v1_us_TSLA_US_{freq}_x.pkl").parent.mkdir(parents=True, exist_ok=True)
        (cache / f"v1_us_TSLA_US_{freq}_x.pkl").write_bytes(b"x")
    args = SimpleNamespace(
        chart_cache=str(cache),
        fund_dirs=[str(tmp_path / "missing_fund")],
        codes="TSLA.US",
    )

    payload = audit_us_selection_sources.build_audit(args)

    assert payload["technical_cache_complete"] is True
    assert payload["fundamental_coverage"]["count"] == 0
    assert payload["selection_system_status"] == {
        "technical": "pass",
        "fundamental": "gap",
        "comparison": "gap",
    }


def test_robustness_audit_separates_strict_registry_from_legacy(tmp_path):
    strict = tmp_path / "us_strict_summary.json"
    legacy = tmp_path / "us_legacy_summary.json"
    _json(
        strict,
        {
            "market": "us",
            "signal_mode": "walk_forward",
            "recursive_l0_min_zs_lines": 3,
            "signal_seen_registry_complete": True,
            "trade_count": 2,
            "no_future_policy": {
                "strict_no_future": True,
                "signal_mode": "walk_forward",
                "signal_seen_registry_complete": True,
                "stale_reappearing_signal_risk": False,
            },
        },
    )
    _json(legacy, {"market": "us", "trade_count": 200, "total_return": 0.2})
    args = SimpleNamespace(
        patterns=[str(tmp_path / "us_*summary.json")],
        min_trades=30,
        include_legacy=True,
    )

    payload = audit_robustness_evidence.build_audit(args)
    by_name = {row["name"]: row for row in payload["rows"]}

    assert by_name[str(strict.name)]["grade"] == "strict_original_registry"
    assert by_name[str(legacy.name)]["grade"] == "legacy_or_unknown"
    assert payload["best_strict_original_registry_trade_count"] == 2
    assert payload["robust_claim_supported"] is False


def test_live_backtest_dedupes_same_identity_to_nearest_structural_boundary():
    import pandas as pd

    from chanlun.recursive_bt import live_backtest
    from chanlun.recursive_bt.engine import Signal

    t = pd.Timestamp("2026-06-02 15:46:00+00:00")
    deduped = live_backtest._dedupe_signals_by_identity(
        [
            Signal(t, 1, "3buy", 225.0, structural_stop_below=201.0, zs_zg=201.0),
            Signal(t, 1, "3buy", 225.0, structural_stop_below=222.0, zs_zg=222.0),
            Signal(t, 1, "3sell", 220.0, structural_stop_above=240.0, zs_zd=240.0),
            Signal(t, 1, "3sell", 220.0, structural_stop_above=231.0, zs_zd=231.0),
        ]
    )

    by_type = {sig.bs_type: sig for sig in deduped}

    assert len(deduped) == 2
    assert by_type["3buy"].structural_stop_below == 222.0
    assert by_type["3sell"].structural_stop_above == 231.0


def _prewarm_args(tmp_path, **overrides):
    base = dict(
        market="us",
        codes="TSLA.US,QQQ.US",
        pool_size=0,
        chart_cache=str(tmp_path / "chart_cache"),
        op_level="1m",
        mid_level="5m",
        big_level="30m",
        start="2026-06-01T13:30:00+00:00",
        end="2026-06-02T20:00:00+00:00",
        signal_mode="walk_forward",
        signal_source="upgrade",
        signal_unit="bi",
        include_l0_upgrade_signals=True,
        recursive_l0_min_zs_lines=3,
        signal_warmup_bars=-1,
        signal_cache_dir=str(tmp_path / "signal_cache"),
        signal_scan_chunk_bars=0,
        trend_dir_warmup_bars=200,
        annotate_nest=False,
        out=str(tmp_path / "manifest.json"),
        resume=True,
        force=False,
        fail_fast=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_prewarm_live_signal_cache_runs_one_symbol_at_a_time(monkeypatch, tmp_path):
    calls = []

    def fake_load_chart_cache_syms(_market, **kwargs):
        codes = tuple(kwargs["codes"])
        calls.append(codes)
        code = codes[0]
        return {
            code: {
                "dates": ["2026-06-01 13:30:00+00:00", "2026-06-01 13:31:00+00:00"],
                "signal_seen_registry_complete": True,
                "signal_seen_registry_first_date": "2026-06-01 13:30:00+00:00",
                "signal_seen_registry_emit_start_idx": 0,
                "signal_cache_stats": {"hits": 0, "misses": 1, "writes": 1, "entries": 2},
                "signal_events": [{"code": code}, {"code": code}],
                "levels": {"op": "1m", "mid": "5m", "big": "30m"},
            }
        }

    monkeypatch.setattr(
        prewarm_live_backtest_signal_cache.live_backtest,
        "load_chart_cache_syms",
        fake_load_chart_cache_syms,
    )

    payload = prewarm_live_backtest_signal_cache.build_prewarm(_prewarm_args(tmp_path))

    assert calls == [("TSLA.US",), ("QQQ.US",)]
    assert payload["summary"]["ok"] == 2
    assert payload["summary"]["registry_complete_all"] is True
    assert payload["summary"]["signal_cache_stats"]["writes"] == 2
    assert Path(tmp_path / "manifest.json").exists()


def test_prewarm_live_signal_cache_resumes_completed_manifest(monkeypatch, tmp_path):
    calls = []

    def fake_load_chart_cache_syms(_market, **kwargs):
        code = kwargs["codes"][0]
        calls.append(code)
        return {
            code: {
                "dates": ["2026-06-01 13:30:00+00:00"],
                "signal_seen_registry_complete": True,
                "signal_cache_stats": {"hits": 0, "misses": 1, "writes": 1, "entries": 1},
                "signal_events": [{"code": code}],
            }
        }

    monkeypatch.setattr(
        prewarm_live_backtest_signal_cache.live_backtest,
        "load_chart_cache_syms",
        fake_load_chart_cache_syms,
    )
    args = _prewarm_args(tmp_path)

    prewarm_live_backtest_signal_cache.build_prewarm(args)

    def explode(*_args, **_kwargs):
        raise AssertionError("completed manifest row should be skipped")

    monkeypatch.setattr(
        prewarm_live_backtest_signal_cache.live_backtest,
        "load_chart_cache_syms",
        explode,
    )
    payload = prewarm_live_backtest_signal_cache.build_prewarm(args)

    assert calls == ["TSLA.US", "QQQ.US"]
    assert payload["summary"]["ok"] == 2
    assert payload["summary"]["skipped"] == 2


def test_walk_forward_signal_scan_checkpoint_resume_matches_full_run(monkeypatch, tmp_path):
    import numpy as np
    import pandas as pd

    from chanlun.recursive_bt import live_backtest
    from chanlun.recursive_bt.engine import Signal

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=7, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(len(dates)) * 10,
            "high": np.ones(len(dates)) * 11,
            "low": np.ones(len(dates)) * 9,
            "close": np.ones(len(dates)) * 10,
            "volume": np.ones(len(dates)),
        }
    )

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        n = len(cd.dates)
        if n == 4:
            return [Signal(cd.dates[-1], 0, "3buy", 10.0)]
        if n == 6:
            return [Signal(cd.dates[-1], 0, "2sell", 10.0)]
        return []

    monkeypatch.setattr(live_backtest, "CL", _CheckpointFakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)
    _CheckpointFakeCL.fail_after = None

    full = live_backtest._walk_forward_signals_by_main_bar(
        "TSLA.US",
        "1m",
        df,
        dates,
        available_delay=pd.Timedelta(0),
        warmup_bars=0,
        signal_cache_dir=str(tmp_path / "full"),
    )

    monkeypatch.setenv("CHANLUN_WF_CHECKPOINT_EVERY", "3")
    _CheckpointFakeCL.fail_after = 3
    with pytest.raises(RuntimeError, match="checkpoint stop"):
        live_backtest._walk_forward_signals_by_main_bar(
            "TSLA.US",
            "1m",
            df,
            dates,
            available_delay=pd.Timedelta(0),
            warmup_bars=0,
            signal_cache_dir=str(tmp_path / "resume"),
        )

    assert list((tmp_path / "resume").glob("*.checkpoint"))

    stats = {}
    _CheckpointFakeCL.fail_after = None
    resumed = live_backtest._walk_forward_signals_by_main_bar(
        "TSLA.US",
        "1m",
        df,
        dates,
        available_delay=pd.Timedelta(0),
        warmup_bars=0,
        signal_cache_dir=str(tmp_path / "resume"),
        signal_cache_stats=stats,
    )

    assert {
        bar: [sig.bs_type for sig in signals]
        for bar, signals in resumed.items()
    } == {
        bar: [sig.bs_type for sig in signals]
        for bar, signals in full.items()
    }
    assert stats["checkpoint_resumes"] == 1
    assert stats["writes"] == 1
    assert not list((tmp_path / "resume").glob("*.checkpoint"))


def test_walk_forward_signal_checkpoint_writes_before_emit_start(monkeypatch, tmp_path):
    import numpy as np
    import pandas as pd

    from chanlun.recursive_bt import live_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=8, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(len(dates)) * 10,
            "high": np.ones(len(dates)) * 11,
            "low": np.ones(len(dates)) * 9,
            "close": np.ones(len(dates)) * 10,
            "volume": np.ones(len(dates)),
        }
    )

    monkeypatch.setattr(live_backtest, "CL", _CheckpointFakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", lambda *_args, **_kwargs: [])
    monkeypatch.setenv("CHANLUN_WF_CHECKPOINT_EVERY", "3")
    _CheckpointFakeCL.fail_after = 3

    with pytest.raises(RuntimeError, match="checkpoint stop"):
        live_backtest._walk_forward_signals_by_main_bar(
            "TSLA.US",
            "1m",
            df,
            dates,
            available_delay=pd.Timedelta(0),
            warmup_bars=-1,
            signal_cache_dir=str(tmp_path),
            emit_start_idx=5,
        )

    assert list(tmp_path.glob("*.checkpoint"))
    _CheckpointFakeCL.fail_after = None


def test_cl_config_extensions_are_covered_by_signal_cache_meta():
    """信号缓存 key 哈希的是全局 CL_CFG（live_backtest._signal_cache_meta 的
    cl_cfg_hash），而非实际传给 CL 的 _cl_config() 字典——只要 _cl_config 相对
    CL_CFG 的全部扩展键都被 meta 单独入 key，缓存就不会静默供旧：
      - recursive_l0_min_zs_lines → meta 恒含同名字段
      - skip_legacy_mmd / skip_legacy_zslx → 由 signal_source 确定性派生，
        signal_source 已入 key
    本测试钉死该不变量：给 _cl_config 新增任何键时必须同步把它显式加进
    _signal_cache_meta（并更新本清单），否则此处失败。（2026-06-12 无未来函数
    审计 LOW#3 加固，避免重蹈「缓存 key 不含影响信号的配置」型未来函数。）
    """
    from chanlun.recursive_bt import live_backtest
    from chanlun.recursive_bt.engine import CL_CFG

    effective = live_backtest._cl_config(
        3, skip_legacy_mmd=True, skip_legacy_zslx=True
    )
    extra = set(effective) - set(CL_CFG)
    keyed = {"recursive_l0_min_zs_lines", "skip_legacy_mmd", "skip_legacy_zslx"}
    assert extra <= keyed, (
        f"_cl_config 新增了未入缓存 key 的配置键: {sorted(extra - keyed)}; "
        "请把它显式加入 _signal_cache_meta 并 bump 缓存版本"
    )
    # 除已单独入 key 的键外，共有键的值不得被 _cl_config 改写（否则 cl_cfg_hash 失真）
    drift = {
        k
        for k in (set(effective) & set(CL_CFG)) - keyed
        if effective[k] != CL_CFG[k]
    }
    assert not drift, (
        f"_cl_config 修改了 CL_CFG 共有键的值但缓存 key 仍哈希原 CL_CFG: {sorted(drift)}"
    )
