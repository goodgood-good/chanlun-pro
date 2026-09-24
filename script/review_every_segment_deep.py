"""A new attributable pass over every latest segment and every pen prefix.

Native clocks are checked on authenticated original OHLC. The additional pen
prefix experiment gives immutable price geometry consecutive integer closure
tokens: those tokens are NEVER described as actual market confirmation times.
Search through confirmation includes feature witnesses past a drawn endpoint.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
import hashlib
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator
from chanlun.cl_utils.price_metadata import (
    strict_snapshot_price_metadata,
    strict_cl_config,
)
from chanlun.screening.cache import input_fingerprint
from chanlun.screening.runner import source_revision
from script.audit_all_segment_results import write_json
from script.review_segment_causal_prefixes import read
from script.review_segment_geometry import geometry, ticks
from script.review_segment_raw_inputs import raw_geometry
from script.check_segment_model import pen, check_evidence
from script.check_segment_unpruned import EveryCandidate
from script.review_segment_confirmation_horizon import inspect_row

PREVIOUS = ROOT / "output/segment_local_gap_fix_20260920"
FRESH = PREVIOUS / "fresh_screening_audit"
OUT = ROOT / "output/segment_deep_review_20260920"


def hashes():
    files = [
        "src/chanlun/core/xd_calculator.py",
        "src/chanlun/core/segment_evidence.py",
        "src/chanlun/core/cl.py",
        "src/chanlun/core/bi_calculator.py",
        "src/chanlun/core/strict_structure/base_profile.py",
        "script/check_segment_model.py",
        "script/check_segment_candidates.py",
        "script/check_segment_unpruned.py",
        "tests/core/test_segment_local_search.py",
        "script/review_segment_confirmation_horizon.py",
        "script/review_every_segment_deep.py",
    ]
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files
    }


def line_tuple(line):
    return (
        line.start_line.index,
        line.end_line.index,
        line.type,
        line.done,
        line.locked_at,
    )


def all_pen_prefixes(actual):
    # Use the engine's actual floating price values. This also exercises its
    # exact comparisons near equality, without rounding a pen to a new value.
    ranked = []
    for i, b in enumerate(actual):
        item = pen(i, b.start.val, b.end.val)
        item.locked_at = i + 1
        ranked.append(item)
    committed, proofs, born = {}, {}, {}
    checks, complete_evidence_checks = 0, 0
    for count in range(3, len(ranked) + 1):
        values = ranked[:count]
        fast, exhaustive = XdCalculator(), EveryCandidate()
        fast.calculate(values)
        exhaustive.calculate(values)
        checks += 1
        states = {tuple(line_tuple(s)[:3]): s.locked_at for s in fast.xds if s.done}
        now_proofs = {p.key: p for p in fast.evidence if p.key in states}
        failures = []
        if any(states.get(k) != at for k, at in committed.items()):
            failures.append("confirmed_pen_prefix_revoked_or_clock_changed")
        if any(now_proofs.get(k) != p for k, p in proofs.items()):
            failures.append("confirmed_construction_evidence_rewritten")
        new = set(states) - set(committed)
        if any(states[k] != count for k in new):
            failures.append("new_confirmation_uses_an_earlier_rank")
        if [line_tuple(s) for s in fast.xds] != [line_tuple(s) for s in exhaustive.xds]:
            failures.append("optional_pruning_changes_geometry_or_closure")
        if fast.evidence != exhaustive.evidence:
            failures.append("optional_pruning_changes_construction_evidence")
        if new:
            try:
                check_evidence(fast, values)
                complete_evidence_checks += 1
            except (AssertionError, ValueError) as exc:
                failures.append("new_proof_independent_reconstruction: " + str(exc))
        if failures:
            return {
                "passed": False,
                "prefixes": checks,
                "failure_prefix_pens": count,
                "failures": failures,
                "prices": [ranked[0].start.val] + [b.end.val for b in values],
                "previous": [(*k, when) for k, when in committed.items()],
                "actual": [line_tuple(s) for s in fast.xds],
                "exhaustive": [line_tuple(s) for s in exhaustive.xds],
                "proofs": [asdict(p) for p in fast.evidence],
            }
        for key in new:
            born[key] = count
        committed.update(states)
        proofs.update(now_proofs)
    return {
        "passed": True,
        "prefixes": checks,
        "independent_evidence_checkpoints": complete_evidence_checks,
        "hypothetical_confirmed_segments": len(committed),
        "birth_ranks": [(*k, n) for k, n in born.items()],
        "clock_scope": "ordinal closure tokens, not market times",
    }


def inspect_dataset(dataset, old_root, digests, revision):
    started = perf_counter()
    result = {
        "id": dataset["id"],
        "code": dataset["code"],
        "frequency": dataset["frequency"],
        "market": dataset["market"],
        "status": "unreviewed",
        "implementation_sha256": digests,
        "source_revision": revision,
        "rows": [],
        "errors": [],
    }
    try:
        old_root = Path(old_root)
        old_clock = read(old_root / "causal_reviews" / f"{dataset['id']}.json.gz")
        source = old_clock["source"]
        raw_path = Path(source["path"])
        assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == source["sha256"], (
            "raw input bytes changed"
        )
        data = pd.read_parquet(raw_path)
        fingerprint = input_fingerprint(data, dataset["code"], dataset["frequency"])
        assert (
            hashlib.sha256(f"{fingerprint}:{revision}".encode()).hexdigest()
            == dataset["calculation_key"]
        ), "raw input key mismatch"
        saved = read(dataset["file"])
        metadata = strict_snapshot_price_metadata(data)
        q = str(metadata.structure_price_quantum)
        config = strict_cl_config(
            structure_price_quantum=metadata.structure_price_quantum,
            price_basis_revision=metadata.price_basis_revision,
        )
        cd = CL(dataset["code"], dataset["frequency"], config, market=dataset["market"])
        cd.process_klines(data, last_bar_closed=True)
        lines, actual_pens = cd.get_xds(), cd.get_contiguous_bis()
        assert [raw_geometry(s, q) for s in lines] == [
            geometry(s, q) for s in saved["chart"]["xds"]
        ], "native rebuilt partition changed"
        cached = {
            (u["start_time"], u["start_tick"], u["end_time"], u["end_tick"]): u
            for u in saved["snapshot"]["screening_segments"]
        }
        for line in lines:
            stamp = (
                int(line.locked_at.timestamp()) if line.locked_at is not None else None
            )
            assert cached[raw_geometry(line, q)[:4]]["confirmed_at"] == stamp, (
                "native market confirmation changed"
            )
        check_evidence(cd.xd_calculator, actual_pens)
        result.update(
            input_authenticated=True,
            source=source,
            bars=len(data),
            pens=len(actual_pens),
            native_segments=len(lines),
            native_confirmed=sum(s.done for s in lines),
        )
        original = read(old_root / "geometry" / f"{dataset['id']}.json.gz")
        real_by_time = {
            (int(p.start.k.date.timestamp()), int(p.end.k.date.timestamp())): p
            for p in actual_pens
        }
        components = {}
        for component in original["components"]:
            pens = []
            for p in component["pens"]:
                actual = real_by_time[(p["start_time"], p["end_time"])]
                assert (ticks(actual.start.val, q), ticks(actual.end.val, q)) == (
                    p["start_tick"],
                    p["end_tick"],
                )
                pens.append(
                    {
                        **p,
                        "confirmed_at": int(actual.locked_at.timestamp())
                        if actual.locked_at is not None
                        else None,
                        "selected_at": int(actual.selected_at.timestamp())
                        if actual.selected_at is not None
                        else None,
                    }
                )
            components[component["component"]] = (component, pens)
        previous = {}
        for row in original["rows"]:
            component, pens = components[row["component"]]
            reviewed = inspect_row(row, component, pens, previous.get(row["component"]))
            unresolved = [
                c
                for c in reviewed["candidates"]
                if c["resolution"] == "needs_individual_reference_and_priority_review"
            ]
            result["rows"].append(
                {
                    **row,
                    "new_checks": {
                        "authenticated_ohlc_rebuild": True,
                        "independent_complete_proof": True,
                        "actual_confirmation_has_available_pen_sources": not reviewed[
                            "errors"
                        ],
                    },
                    "confirmation_horizon": reviewed["horizon"],
                    "candidate_scan": reviewed,
                    "unresolved_candidates": unresolved,
                    "verdict": "needs_individual_review"
                    if unresolved or reviewed["errors"]
                    else "pending_tail"
                    if not row["confirmed"]
                    else "supported",
                }
            )
            if reviewed["errors"]:
                result["errors"].append(
                    {"segment": row["number"], "errors": reviewed["errors"]}
                )
            previous[row["component"]] = row
        result["pen_prefix_review"] = all_pen_prefixes(actual_pens)
        if not result["pen_prefix_review"]["passed"]:
            result["errors"].append(
                {
                    "kind": "pen_prefix_failure",
                    "details": result["pen_prefix_review"]["failures"],
                }
            )
        for row in result["rows"]:
            row["new_checks"]["every_pen_prefix_and_unpruned_search"] = result[
                "pen_prefix_review"
            ]["passed"]
        result["status"] = (
            "failed"
            if result["errors"]
            else "needs_individual_review"
            if any(r["unresolved_candidates"] for r in result["rows"])
            else "passed"
        )
    except Exception as exc:
        result["status"] = "input_or_review_failure"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    result["seconds"] = round(perf_counter() - started, 4)
    write_json(OUT / "datasets" / f"{dataset['id']}.json.gz", result)
    return {
        k: v for k, v in result.items() if k not in {"rows", "pen_prefix_review"}
    } | {
        "segments": len(result["rows"]),
        "pen_prefixes": result.get("pen_prefix_review", {}).get("prefixes", 0),
        "candidate_counts": dict(
            sum(
                (Counter(r["candidate_scan"]["counts"]) for r in result["rows"]),
                Counter(),
            )
        ),
        "unresolved_candidates": sum(
            len(r["unresolved_candidates"]) for r in result["rows"]
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    inv = read(FRESH / "inventory_with_inputs.json")
    jobs = [(d, FRESH) for d in inv["datasets"]] + [
        (d, PREVIOUS) for d in read(PREVIOUS / "supplement.json")["datasets"]
    ]
    jobs.sort(
        key=lambda item: (
            not item[0].get("supplement"),
            item[0]["code"] != "QQQ.US",
            item[0]["code"] not in {"BTC/USDT", "SH.688506"},
            item[0]["code"],
            item[0]["frequency"],
        )
    )
    if args.limit:
        jobs = jobs[: args.limit]
    digests, revision = hashes(), source_revision()
    assert revision == inv["reference_source_revision"], (
        "runtime changed since saved results"
    )
    rows, pending = [], []
    for d, folder in jobs:
        path = OUT / "datasets" / f"{d['id']}.json.gz"
        if args.resume and path.exists():
            old = read(path)
            if old.get("implementation_sha256") == digests and old.get("status") in {
                "passed",
                "needs_individual_review",
            }:
                rows.append(
                    {
                        k: v
                        for k, v in old.items()
                        if k not in {"rows", "pen_prefix_review"}
                    }
                    | {
                        "segments": len(old["rows"]),
                        "pen_prefixes": old["pen_prefix_review"]["prefixes"],
                        "candidate_counts": dict(
                            sum(
                                (
                                    Counter(r["candidate_scan"]["counts"])
                                    for r in old["rows"]
                                ),
                                Counter(),
                            )
                        ),
                        "unresolved_candidates": sum(
                            len(r["unresolved_candidates"]) for r in old["rows"]
                        ),
                    }
                )
                continue
        pending.append((d, folder))
    print(
        {"phase": "new_deep_review", "windows": len(jobs), "pending": len(pending)},
        flush=True,
    )
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for task in as_completed(
            [
                pool.submit(inspect_dataset, d, folder, digests, revision)
                for d, folder in pending
            ]
        ):
            row = task.result()
            rows.append(row)
            if (
                row["status"] != "passed"
                or len(rows) % 100 == 0
                or len(rows) == len(jobs)
            ):
                print(
                    {
                        "done": len(rows),
                        "total": len(jobs),
                        "states": dict(Counter(r["status"] for r in rows)),
                        "last": {
                            k: row[k]
                            for k in [
                                "code",
                                "frequency",
                                "status",
                                "errors",
                                "unresolved_candidates",
                            ]
                        },
                    },
                    flush=True,
                )
    assert hashes() == digests and source_revision() == revision, (
        "implementation changed during audit"
    )
    summary = {
        "finished_at": datetime.now().astimezone().isoformat(),
        "reference_run": inv["reference_run"],
        "source_revision": revision,
        "implementation_sha256": digests,
        "completed": len(rows),
        "datasets": rows,
        "states": dict(Counter(r["status"] for r in rows)),
        **{
            key: sum(r.get(key, 0) for r in rows)
            for key in [
                "segments",
                "native_confirmed",
                "bars",
                "pens",
                "pen_prefixes",
                "unresolved_candidates",
            ]
        },
        "candidate_counts": dict(
            sum((Counter(r["candidate_counts"]) for r in rows), Counter())
        ),
    }
    write_json(OUT / ("pilot.json" if args.limit else "all_results.json"), summary)
    print(
        {
            k: v
            for k, v in summary.items()
            if k not in {"datasets", "implementation_sha256"}
        },
        flush=True,
    )
    if any(r["status"] in {"failed", "input_or_review_failure"} for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
