from __future__ import annotations

import hashlib
from pathlib import Path
import pickle
from types import SimpleNamespace

import pytest

from tools.backtest_v3_sector_first_full_market import (
    _direct_checkpoint_binding,
    _load_bound_direct_pickle,
    _require_current_direct_algorithm,
)
from tools.extract_v3_sector_first_direct_facts import _checkpoint_binding
from chanlun.decision_support.trading_system.v3_recent_year_provenance import (
    RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
    recent_year_research_algorithm_hashes,
    recent_year_research_algorithm_revision,
)


SHA_PREFIX = "sha256:"


def _manifest(*, code: str, payload: bytes) -> dict[str, object]:
    return {
        "schema": "chanlun-v3-sector-first-direct-extract/v3",
        "symbols": {
            code: {
                "code": code,
                **_checkpoint_binding(code, payload),
            }
        },
    }


def test_checkpoint_binding_hashes_the_exact_serialized_bytes() -> None:
    payload = pickle.dumps({"value": 1}, protocol=pickle.HIGHEST_PROTOCOL)

    value = _checkpoint_binding("SH.600000", payload)

    assert value == {
        "checkpoint_path": "direct_symbols/SH_600000.pkl",
        "checkpoint_sha256": (
            SHA_PREFIX + hashlib.sha256(payload).hexdigest()
        ),
        "checkpoint_size_bytes": len(payload),
    }


def test_replay_verifies_checkpoint_before_unpickling(tmp_path: Path) -> None:
    path = tmp_path / "SH_600000.pkl"
    payload = pickle.dumps({"value": 1}, protocol=pickle.HIGHEST_PROTOCOL)
    path.write_bytes(payload)
    binding = _checkpoint_binding("SH.600000", payload)

    assert _load_bound_direct_pickle(
        path,
        dict,
        expected_sha256=str(binding["checkpoint_sha256"]),
        expected_size_bytes=int(binding["checkpoint_size_bytes"]),
    ) == {"value": 1}

    # A valid pickle of the same semantic type is still a different causal
    # input and must be rejected before its object is consumed.
    path.write_bytes(
        pickle.dumps({"value": 2}, protocol=pickle.HIGHEST_PROTOCOL)
    )
    with pytest.raises(ValueError, match="checkpoint (size|SHA256) changed"):
        _load_bound_direct_pickle(
            path,
            dict,
            expected_sha256=str(binding["checkpoint_sha256"]),
            expected_size_bytes=int(binding["checkpoint_size_bytes"]),
        )


def test_manifest_binding_rejects_legacy_path_and_type_confusion() -> None:
    code = "SZ.000001"
    payload = pickle.dumps({"value": 1}, protocol=pickle.HIGHEST_PROTOCOL)
    valid = _manifest(code=code, payload=payload)

    assert _direct_checkpoint_binding(valid, code) == (
        "direct_symbols/SZ_000001.pkl",
        SHA_PREFIX + hashlib.sha256(payload).hexdigest(),
        len(payload),
    )

    legacy = {**valid, "schema": "chanlun-v3-sector-first-direct-extract/v2"}
    with pytest.raises(RuntimeError, match="lacks checkpoint bindings"):
        _direct_checkpoint_binding(legacy, code)

    escaped = _manifest(code=code, payload=payload)
    escaped["symbols"][code]["checkpoint_path"] = "../outside.pkl"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="checkpoint path changed"):
        _direct_checkpoint_binding(escaped, code)

    confused = _manifest(code=code, payload=payload)
    confused["symbols"][code]["checkpoint_size_bytes"] = True  # type: ignore[index]
    with pytest.raises(RuntimeError, match="checkpoint binding is invalid"):
        _direct_checkpoint_binding(confused, code)


def test_replay_rejects_a_self_consistent_but_stale_algorithm_manifest() -> None:
    root = Path(__file__).resolve().parents[2]
    hashes = recent_year_research_algorithm_hashes(root)
    revision = recent_year_research_algorithm_revision(hashes)
    manifest: dict[str, object] = {
        "algorithm": {
            "scope": RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
            "revision": revision,
            "hashes": tuple(
                {"path": path, "sha256": digest}
                for path, digest in hashes
            ),
        }
    }
    trigger = SimpleNamespace(algorithm_revision=revision)

    _require_current_direct_algorithm(manifest, trigger)  # type: ignore[arg-type]

    stale = {
        **manifest,
        "algorithm": {
            **manifest["algorithm"],  # type: ignore[dict-item]
            "revision": "sha256:" + "0" * 64,
        },
    }
    stale_trigger = SimpleNamespace(algorithm_revision="sha256:" + "0" * 64)
    with pytest.raises(RuntimeError, match="differs from current decision source"):
        _require_current_direct_algorithm(  # type: ignore[arg-type]
            stale,
            stale_trigger,
        )
