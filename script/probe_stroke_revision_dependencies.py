"""Audit downstream behavior at every confirmed-stroke revision in a saved probe.

Each case compares the prefix immediately before the event, one appended bar,
and a fresh calculation of the same final prefix. This does not replace an
all-prefix replay and does not classify every revision as a source-rule error.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import (
    STRICT_STROKE_MODE, strict_base_config, strict_base_config_revision,
)
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.unit_adapter import adapt_lines


def snapshot(cd):
    result = cd.get_native_centers()
    units = adapt_lines(cd.get_xds(), 0, SourceKind.SEGMENT, "0.01",
                        cd._strict_as_of(), cd._strict_registry(),
                        constituent_lines=cd.get_bis())
    confirmed = {
        (line.start.k.k_index, line.end.k.k_index, line.start.val, line.end.val):
        dict(unit_id=unit.unit_id, formed_at=line.formed_at, locked_at=line.locked_at)
        for line, unit in zip(cd.get_xds(), units) if line.is_done()
    }
    return result, confirmed


def _center_core_geometry(center):
    units = tuple((u.direction, u.market_start, u.market_end,
                   u.start_tick, u.end_tick, u.low_tick, u.high_tick)
                  for u in center.initial_units)
    return (center.structural_level, center.source_kind, center.price_basis_revision,
            center.zd_tick, center.zg_tick, center.body_start_market_time,
            center.established_market_time, units)


def _center_record(center):
    return dict(center_id=center.center_id, state=center.state,
                core_geometry=_center_core_geometry(center),
                dd_tick=center.dd_tick, gg_tick=center.gg_tick,
                established_at=center.established_at, completed_at=center.completed_at)


def center_revisions(before, after):
    """Distinguish a retired ID from disappearance of its initial center core.

    Matching cores do not assert that the entire extended body is unchanged;
    its outer range and evidence times are also recorded for comparison.
    """
    current = {c.center_id for c in after.centers}
    return [dict(before=_center_record(old),
                 same_core_replacements=[_center_record(new) for new in after.centers
                                         if _center_core_geometry(new) == _center_core_geometry(old)])
            for old in before.centers if old.center_id not in current]


def check_event(frame, prefix, code, frequency, config):
    market = "us" if code.endswith("US") else "a"
    live = CL(code, frequency, config, market=market)
    live.process_klines(frame.head(prefix - 1))
    before, old_units = snapshot(live)
    registry = live._strict_registry()
    old_confirmations = dict(registry._confirmed_at)
    row = frame.iloc[prefix - 1]
    live.process_kline_values(row.date, row.open, row.high, row.low, row.close, row.volume)
    after, new_units = snapshot(live)
    cold = CL(code, frequency, config, market=market)
    cold.process_klines(frame.head(prefix))
    cold_result, cold_units = snapshot(cold)
    errors = []
    if after != cold_result:
        errors.append("native_center_replay_mismatch")
    if new_units != cold_units:
        errors.append("confirmed_segment_replay_mismatch")
    if any(registry._confirmed_at.get(key) != value for key, value in old_confirmations.items()):
        errors.append("previous_confirmation_record_changed_or_removed")
    changed = [dict(endpoints=key, before=value, after=new_units[key])
               for key, value in old_units.items()
               if key in new_units and value != new_units[key]]
    return dict(
        raw_prefix=prefix, observed_at=row.date,
        removed_confirmed_segments=[key for key in old_units if key not in new_units],
        changed_confirmed_segments=changed,
        removed_formal_centers=sorted({c.center_id for c in before.centers}
                                     - {c.center_id for c in after.centers}),
        center_revisions=center_revisions(before, after),
        errors=errors,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only-prefixes", type=int, nargs="+")
    args = parser.parse_args()
    revisions = json.loads(args.revisions.read_text(encoding="utf-8"))
    if revisions["profile_revision"] != strict_base_config_revision():
        raise ValueError("revision input must match the current production profile")
    config = dict(strict_base_config(), structure_price_quantum="0.01",
                  price_basis_revision="probe-raw", strict_config_revision="probe-v7")
    report = dict(stroke_profile=STRICT_STROKE_MODE,
                  profile_revision=strict_base_config_revision(),
                  input=str(args.revisions), only_prefixes=args.only_prefixes, markets=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for source in revisions["markets"]:
        path = Path(source["input"])
        if not path.is_absolute():
            path = ROOT / path
        frame = pd.read_parquet(path)
        code, frequency = path.stem.rsplit("_", 1)
        cases = []
        events = [event for event in source["events"]
                  if args.only_prefixes is None or event["raw_prefix"] in args.only_prefixes]
        for event in events:
            prefix = event["raw_prefix"]
            try:
                case = check_event(frame, prefix, code, frequency, config)
            except Exception as exc:
                case = dict(raw_prefix=prefix, errors=[f"{type(exc).__name__}: {exc}"])
            cases.append(case)
            if len(cases) % 25 == 0:
                print(f"{path.name}: {len(cases)}/{len(events)}", flush=True)
        report["markets"].append(dict(input=str(path), cases=cases))
        args.output.write_text(json.dumps(report, default=str, indent=2) + "\n", encoding="utf-8")
        print(f"{path.name}: cases={len(cases)} errors={sum(bool(c['errors']) for c in cases)}", flush=True)
    return int(any(case["errors"] for market in report["markets"] for case in market["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
