"""Freeze and audit every saved native segment, with one record per segment.

Saved pen geometry does not contain pen confirmation timestamps. Geometry-only
records explicitly leave that temporal check open; raw OHLC replay is a
separate, attributable layer. Existing production helpers are never called a
separate original-theory oracle.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sys
from time import sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from script.audit_qqq_1m_segments import source_evidence

OUT = ROOT / "output/segment_by_segment_20260919"
DATA = Path("D:/chanlun_pro")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    path.write_bytes(gzip.compress(raw) if path.name.endswith(".gz") else raw)


def implementation():
    names = ["src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
             "src/chanlun/core/strict_structure/base_profile.py", "script/check_segment_model.py"]
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def freeze_one(item, output):
    path, origin = item
    path, output = Path(path), Path(output)
    for attempt in range(8):
        try:
            raw = path.read_bytes()
            break
        except PermissionError:
            if attempt == 7:
                raise
            sleep(.025 * (attempt + 1))
    wrapper = json.loads(gzip.decompress(raw))
    if hashlib.sha256(wrapper["payload"].encode()).hexdigest() != wrapper["sha256"]:
        raise ValueError(f"Invalid saved calculation checksum: {path}")
    payload = json.loads(wrapper["payload"])
    snap = payload["snapshot"]
    identity = hashlib.sha256((snap["symbol"] + "|" + snap["source_frequency"] + "|" + payload["key"]).encode()).hexdigest()[:24]
    frozen = {"calculation_key": payload["key"], "snapshot": {
        k: v for k, v in snap.items() if k in {
            "symbol", "source_frequency", "source_closed_at", "source_started_at", "price_basis_revision",
            "structure_price_quantum", "strict_config_revision", "structure_revision", "snapshot_revision",
            "screening_segments", "stroke_connection_pending", "conditional_centers"}},
        "chart": {k: snap["chart"][k] for k in ["bis", "xds", "fxs"]},
        "source": {"file": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "origin": origin}}
    source_input=payload.get('source_input')
    authenticated_input=None
    if source_input:
        original=(path.parent/source_input['file']).resolve()
        if not original.is_relative_to((path.parent/'inputs').resolve()):
            raise ValueError('Saved calculation input is outside its input store')
        if hashlib.sha256(original.read_bytes()).hexdigest()!=source_input['sha256']:
            raise ValueError('Saved calculation input changed before the audit freeze')
        authenticated_input={**source_input,'file':str(original)}
        frozen['source_input']=authenticated_input
    target = output / "snapshots" / origin / (identity + ".json.gz")
    write_json(target, frozen)
    result={"id": identity, "file": str(target), "code": snap["symbol"], "frequency": snap["source_frequency"],
            "calculation_key": payload["key"], "snapshot_revision": snap["snapshot_revision"],
            "source_started_at": snap.get("source_started_at"), "source_closed_at": snap["source_closed_at"],
            "pens": len(snap["chart"]["bis"]), "segments": len(snap["chart"]["xds"]),
            "source": frozen["source"]}
    if authenticated_input:
        result.update(saved_raw_file=authenticated_input['file'],saved_raw_sha256=authenticated_input['sha256'],
                      expected_raw_fingerprint=authenticated_input['fingerprint'])
    return result


def freeze(output, workers):
    output.mkdir(parents=True, exist_ok=True)
    run_id = read_json(DATA / "screening/latest.json")["run_id"]
    run = DATA / "screening" / run_id
    universe = read_json(run / "universe.json")
    status = read_json(run / "status.json")
    if status["status"] != "completed":
        raise ValueError("The reference screening must have completed before its results are audited")
    raw_results = [json.loads(x) for x in (run / "results.jsonl").read_bytes().splitlines()]
    markets = {s["code"]: s.get("market", "a") for s in universe}
    jobs = [(str(p), "screening") for p in sorted((DATA / "screening/analysis_cache").glob("*.json.gz"))]
    jobs += [(str(p), "monitor") for p in sorted((DATA / "signal_monitor/runs/analysis_cache").glob("*.json.gz"))]
    begun, rows, failures = datetime.now().astimezone().isoformat(), {}, []
    print(json.dumps({"phase": "freeze", "universe": len(universe), "source_files": len(jobs)}), flush=True)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(freeze_one, item, output): item for item in jobs}
        for n, future in enumerate(as_completed(futures), 1):
            try:
                d = future.result()
                d["market"] = markets.get(d["code"], "us" if d["code"].endswith(".US") else "a")
                if d["id"] in rows:
                    rows[d["id"]]["sources"].append(d["source"])
                else:
                    d["sources"] = [d.pop("source")]
                    rows[d["id"]] = d
            except Exception as exc:
                failures.append({"file": futures[future][0], "error": repr(exc)})
            if n % 200 == 0:
                print(json.dumps({"phase": "freeze", "files_done": n, "files": len(jobs), "unique": len(rows), "failures": len(failures)}), flush=True)
    by_code = defaultdict(list)
    for d in rows.values():
        by_code[(d["market"], d["code"])].append(d["id"])
    result_by_code = {(r.get("market", "a"), r["code"]): r for r in raw_results}
    asset_rows = []
    for stock in universe:
        key = stock.get("market", "a"), stock["code"]
        saved = result_by_code[key]
        asset_rows.append({**stock, "dataset_ids": by_code[key], "screening_error": saved.get("error"),
                           "data_diagnostics": [{"frequency": r["frequency"], "errors": r.get("data_errors", []),
                                                 "error": r.get("error"), "quality": r.get("data_quality")}
                                                for r in saved["rows"] if r.get("data_errors")],
                           "coverage": "saved_segments_available" if by_code[key] else "no_saved_segment_result"})
    expected = {}
    for r in raw_results:
        for row in r["rows"]:
            expected[(r.get("market", "a"), r["code"], row["frequency"])] = row
    for d in rows.values():
        row = expected.get((d["market"], d["code"], d["frequency"]), {})
        if d["frequency"] == "1m":
            row = expected.get((d["market"], d["code"], "5m"), {})
            evidence = row.get("confirmation_evidence", {})
            fingerprint = evidence.get("input_fingerprint")
        else:
            evidence = row.get("evidence", {})
            fingerprint = row.get("input_fingerprint")
        if fingerprint:
            reference_key = hashlib.sha256(f"{fingerprint}:{status['source_revision']}".encode()).hexdigest()
            d["matches_reference_input_key"] = d["calculation_key"] == reference_key
        else:
            d["matches_reference_input_key"] = None
        from chanlun.screening.markets import storage_symbol
        raw_path = run / "evidence" / f"{storage_symbol(d['code'], d['market'])}_{d['frequency']}.parquet"
        if not d.get('saved_raw_file') and raw_path.is_file() and evidence.get("input_fingerprint"):
            d["saved_raw_file"] = str(raw_path)
            d["saved_raw_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            d["expected_raw_fingerprint"] = evidence["input_fingerprint"]
    manifest = {"frozen_at": begun, "reference_run": run_id, "request": read_json(run / "request.json"),
                "universe": asset_rows, "source_files": len(jobs), "datasets": sorted(rows.values(), key=lambda d: (d["market"], d["code"], d["frequency"])),
                "freeze_failures": failures, "segments": sum(d["segments"] for d in rows.values()),
                "pens": sum(d["pens"] for d in rows.values()), "implementation_sha256": implementation(),
                "raw_clock_check": "pending; pen confirmation times are absent from saved render geometry"}
    write_json(output / "inventory.json", manifest)
    write_json(output / "original_sources.json", source_evidence())
    print(json.dumps({"phase": "frozen", "assets": len(asset_rows), "assets_with_results": sum(bool(a["dataset_ids"]) for a in asset_rows),
                      "datasets": len(rows), "segments": manifest["segments"], "pens": manifest["pens"], "failures": len(failures)}), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=OUT)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--freeze", action="store_true")
    args = p.parse_args()
    if args.freeze:
        freeze(args.output, args.workers)
    else:
        raise ValueError("Select an audit phase")


if __name__ == "__main__":
    main()
