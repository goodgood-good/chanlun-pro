#!/usr/bin/env python3
"""Rebuild a missing immutable membership index from an exact checkpoint tree.

This recovery is deliberately offline: it never imports QMT or accesses the
network.  The rebuilt index is published only when its checkpoint-tree digest
matches an independently supplied digest from an existing certified PIT proof.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    qmt_native_code,
)
from tools.snapshot_qmt_pit_metadata import (
    _atomic_json,
    _load_complete_checkpoint_inventory,
    _membership_index_payload,
    _tree_hash,
)


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--checkpoint-dir", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--not-after", type=_iso_date, required=True)
    result.add_argument(
        "--expected-checkpoint-tree-sha256",
        required=True,
        help="digest copied from an existing certified PIT scope proof",
    )
    return result


def _checkpoint_codes(directory: Path) -> tuple[str, ...]:
    codes: list[str] = []
    for path in sorted(directory.glob("*.json"), key=lambda value: value.name):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid membership checkpoint: {path.name}") from exc
        code = str(raw.get("code") or "") if isinstance(raw, dict) else ""
        qmt_native_code(code)
        codes.append(code)
    if not codes or len(codes) != len(set(codes)):
        raise ValueError(
            "membership checkpoint identities must be non-empty and unique"
        )
    return tuple(sorted(codes))


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    expected = str(args.expected_checkpoint_tree_sha256)
    if _SHA256.fullmatch(expected) is None:
        raise ValueError("expected checkpoint-tree digest must be sha256")
    checkpoint_dir = args.checkpoint_dir.resolve()
    if not checkpoint_dir.is_dir():
        raise ValueError("checkpoint directory does not exist")
    output = args.output.resolve()
    if output == checkpoint_dir or checkpoint_dir in output.parents:
        raise ValueError("membership index output must be outside checkpoint tree")
    codes = _checkpoint_codes(checkpoint_dir)
    checkpoint_paths = tuple(
        sorted(checkpoint_dir.glob("*.json"), key=lambda value: value.name)
    )
    certified_tree = _tree_hash(checkpoint_paths, root=checkpoint_dir)
    if certified_tree != expected:
        raise RuntimeError(
            "checkpoint files do not match the certified PIT proof: "
            f"expected {expected}, got {certified_tree}"
        )
    memberships, paths = _load_complete_checkpoint_inventory(
        inventory_codes=codes,
        checkpoint_dir=checkpoint_dir,
        end=args.not_after,
    )
    payload = _membership_index_payload(
        memberships=memberships,
        checkpoint_paths=paths,
        checkpoint_root=checkpoint_dir,
        end=args.not_after,
    )
    index_tree = str(payload["checkpoint_tree_sha256"])
    _atomic_json(output, payload)
    print(
        json.dumps(
            {
                "complete": True,
                "checkpoint_count": len(codes),
                "membership_count": len(memberships),
                "certified_file_tree_sha256": certified_tree,
                "index_entry_tree_sha256": index_tree,
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
