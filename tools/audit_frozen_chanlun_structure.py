#!/usr/bin/env python3
"""Record and verify the frozen Chanlun structure core without mutating it."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Sequence

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config


SCHEMA = "chanlun-frozen-structure-audit/v1"
DEFAULT_BASELINE = Path(
    "audit/chanlun_live_integration/frozen_structure_baseline.json"
)
DEFAULT_VERIFICATION = Path(
    "audit/chanlun_live_integration/frozen_structure_verification.json"
)
DEFAULT_BAR_COUNTS = (3000, 5000)


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _symbol_manifest(path: Path) -> list[dict[str, object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rows: list[dict[str, object]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rows.append(
                {"kind": "function", "name": node.name, "line": node.lineno}
            )
        elif isinstance(node, ast.ClassDef):
            methods = [
                {"name": child.name, "line": child.lineno}
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            rows.append(
                {
                    "kind": "class",
                    "name": node.name,
                    "line": node.lineno,
                    "methods": methods,
                }
            )
    return rows


def _core_files() -> list[dict[str, object]]:
    root = SOURCE_ROOT / "chanlun" / "core"
    output: list[dict[str, object]] = []
    for path in sorted(root.rglob("*.py")):
        output.append(
            {
                "path": path.relative_to(PROJECT_ROOT).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
                "symbols": _symbol_manifest(path),
            }
        )
    return output


def _deterministic_klines(bar_count: int) -> pd.DataFrame:
    rng = np.random.RandomState(7)
    start = pd.Timestamp("2024-01-01 09:30:00", tz="Asia/Shanghai")
    rows: list[dict[str, object]] = []
    price = 100.0
    previous_high = 100.3
    previous_low = 99.7
    for index in range(bar_count):
        price += rng.randn() * 0.6 + 0.4 * np.sin(index / 9.0)
        price = max(price, 5.0)
        high = price + 0.25
        low = price - 0.25
        if index % 11 == 10:
            high = max(high, previous_high) + 0.15
            low = min(low, previous_low) - 0.15
        rows.append(
            {
                "date": start + pd.Timedelta(minutes=index),
                "high": high,
                "low": low,
                "open": price,
                "close": price,
                "volume": 1000.0,
            }
        )
        previous_high, previous_low = high, low
    return pd.DataFrame(rows)


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


def _endpoint_time(endpoint: object) -> str | None:
    if endpoint is None:
        return None
    representative = getattr(endpoint, "k", None)
    return _timestamp(getattr(representative, "date", None))


def _line_signature(values: Sequence[object]) -> list[list[object]]:
    return [
        [
            getattr(value, "index", None),
            getattr(value, "type", None),
            _endpoint_time(getattr(value, "start", None)),
            _endpoint_time(getattr(value, "end", None)),
            getattr(value, "high", None),
            getattr(value, "low", None),
            getattr(value, "done", None),
        ]
        for value in values
    ]


def _fractal_signature(values: Sequence[object]) -> list[list[object]]:
    return [
        [
            getattr(value, "index", None),
            getattr(value, "type", None),
            _endpoint_time(value),
            getattr(value, "val", None),
            getattr(value, "done", None),
        ]
        for value in values
    ]


def _center_signature(values: Sequence[object]) -> list[list[object]]:
    return [
        [
            getattr(value, "index", None),
            getattr(value, "type", None),
            getattr(value, "zd", None),
            getattr(value, "zg", None),
            getattr(value, "dd", None),
            getattr(value, "gg", None),
            getattr(value, "done", None),
            getattr(value, "real", None),
        ]
        for value in values
    ]


def _strict_level_signature(level: object) -> dict[str, object]:
    center_result = level.center_result
    return {
        "structural_level": level.structural_level,
        "unit_ids": [value.unit_id for value in level.units],
        "center_ids": [value.center_id for value in center_result.centers],
        "previews": [
            [
                value.state.value,
                list(value.unit_ids),
                value.zd_tick,
                value.zg_tick,
                _timestamp(value.available_at),
            ]
            for value in center_result.previews
        ],
        "trend_ids": [value.trend_id for value in level.trend_types],
        "completed_trend_ids": [
            value.trend_id for value in level.completed_trends
        ],
    }


def _strict_point_signature(value: object) -> list[object]:
    return [
        value.point_id,
        value.point_type,
        value.structural_level,
        value.variant.value,
        _timestamp(value.anchor_at),
        _timestamp(value.confirmed_at),
        _timestamp(value.available_at),
        value.center_id,
        value.anchor_tick,
        value.invalidation_tick,
    ]


def _representative_output(bar_count: int) -> dict[str, object]:
    state = CL(
        "SZ.000001",
        "1m",
        {
            **strict_base_config(),
            "structure_price_quantum": "0.01",
            "price_basis_revision": "audit-raw-v1",
            "skip_legacy_zslx": True,
            "skip_legacy_mmd": True,
        },
    )
    state.process_klines(_deterministic_klines(bar_count))
    evidence = state.get_strict_evidence()
    payload: dict[str, object] = {
        "bar_count": bar_count,
        "source_closed_at": _timestamp(state.get_src_klines()[-1].date),
        "strict_config_revision": evidence.strict_config_revision,
        "structure_revision": evidence.structure_revision,
        "processed_kline_signature": [
            [
                value.index,
                _timestamp(value.date),
                value.h,
                value.l,
            ]
            for value in state.get_cl_klines()
        ],
        "fractal_signature": _fractal_signature(state.get_fxs()),
        "stroke_signature": _line_signature(state.get_bis()),
        "segment_signature": _line_signature(state.get_xds()),
        "stroke_center_signature": _center_signature(state.get_bi_zss()),
        "segment_center_signature": _center_signature(state.get_xd_zss()),
        "strict_levels": [
            _strict_level_signature(level) for level in evidence.structure.levels
        ],
        "confirmed_points": [
            _strict_point_signature(value) for value in evidence.confirmed_points
        ],
        "approaching_points": [
            _strict_point_signature(value) for value in evidence.approaching_points
        ],
        "divergence_ids": [value.divergence_id for value in evidence.divergences],
    }
    payload["output_sha256"] = _sha256_bytes(
        _canonical_json(payload).encode("utf-8")
    )
    return payload


def build_contract() -> dict[str, object]:
    files = _core_files()
    outputs = [_representative_output(value) for value in DEFAULT_BAR_COUNTS]
    contract: dict[str, object] = {
        "frozen_scope": "src/chanlun/core/**/*.py",
        "pen_definition_mode": "ORIGINAL_OLD_PEN",
        "files": files,
        "representative_outputs": outputs,
    }
    contract["core_contract_sha256"] = _sha256_bytes(
        _canonical_json(contract).encode("utf-8")
    )
    return contract


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    result.add_argument(
        "--verification-output",
        type=Path,
        default=DEFAULT_VERIFICATION,
    )
    action = result.add_mutually_exclusive_group(required=True)
    action.add_argument("--write-baseline", action="store_true")
    action.add_argument("--verify-baseline", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    path = args.baseline.resolve()
    contract = build_contract()
    if args.write_baseline:
        payload = {
            "schema": SCHEMA,
            "captured_at": datetime.now().astimezone().isoformat(),
            "workspace_revision": _git_revision(),
            "core_contract": contract,
        }
        _atomic_json(path, payload)
        print(
            json.dumps(
                {
                    "status": "baseline_written",
                    "path": str(path),
                    "file_count": len(contract["files"]),
                    "core_contract_sha256": contract["core_contract_sha256"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    baseline = json.loads(path.read_text(encoding="utf-8"))
    expected = baseline.get("core_contract")
    matches = expected == contract
    expected_files = {
        row["path"]: row
        for row in expected.get("files", ())
    } if isinstance(expected, dict) else {}
    actual_files = {row["path"]: row for row in contract["files"]}
    file_paths = tuple(sorted(set(expected_files) | set(actual_files)))
    file_verification = tuple(
        {
            "path": file_path,
            "before_sha256": expected_files.get(file_path, {}).get("sha256"),
            "after_sha256": actual_files.get(file_path, {}).get("sha256"),
            "unchanged": (
                expected_files.get(file_path, {}).get("sha256")
                == actual_files.get(file_path, {}).get("sha256")
                and file_path in expected_files
                and file_path in actual_files
            ),
        }
        for file_path in file_paths
    )
    expected_outputs = (
        expected.get("representative_outputs", ())
        if isinstance(expected, dict)
        else ()
    )
    actual_outputs = contract["representative_outputs"]
    output_verification = tuple(
        {
            "bar_count": actual_row.get("bar_count"),
            "before_output_sha256": expected_row.get("output_sha256"),
            "after_output_sha256": actual_row.get("output_sha256"),
            "unchanged": (
                expected_row.get("output_sha256")
                == actual_row.get("output_sha256")
            ),
        }
        for expected_row, actual_row in zip(expected_outputs, actual_outputs)
    )
    verification = {
        "schema": "chanlun-frozen-structure-verification/v1",
        "verified_at": datetime.now().astimezone().isoformat(),
        "status": "PASS_ZERO_CHANGE" if matches else "FAIL_CHANGED",
        "frozen_scope": contract["frozen_scope"],
        "file_count": len(contract["files"]),
        "before_core_contract_sha256": (
            None
            if not isinstance(expected, dict)
            else expected.get("core_contract_sha256")
        ),
        "after_core_contract_sha256": contract["core_contract_sha256"],
        "files": file_verification,
        "representative_outputs": output_verification,
        "all_files_unchanged": all(row["unchanged"] for row in file_verification),
        "all_representative_outputs_unchanged": (
            len(output_verification) == len(actual_outputs)
            and len(expected_outputs) == len(actual_outputs)
            and all(row["unchanged"] for row in output_verification)
        ),
    }
    verification_path = args.verification_output.resolve()
    _atomic_json(verification_path, verification)
    print(
        json.dumps(
            {
                "status": "passed" if matches else "failed",
                "path": str(path),
                "expected_sha256": (
                    None if not isinstance(expected, dict) else expected.get("core_contract_sha256")
                ),
                "actual_sha256": contract["core_contract_sha256"],
                "file_count": len(contract["files"]),
                "verification_output": str(verification_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if matches else 3


def _git_revision() -> str:
    head = PROJECT_ROOT / ".git" / "HEAD"
    if not head.exists():
        return "UNRESOLVED"
    value = head.read_text(encoding="utf-8").strip()
    if value.startswith("ref: "):
        target = PROJECT_ROOT / ".git" / value[5:]
        if target.exists():
            return target.read_text(encoding="utf-8").strip()
    return value


if __name__ == "__main__":
    raise SystemExit(main())
