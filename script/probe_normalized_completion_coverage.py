"""Read-only probe for an existing completion route omitted by interior search.

Production is untouched. Reuses its established-predecessor certificate only
inside already permitted first-case competition; preserves F2 priority and
existing witness deadlines. Returned proofs are independently checked.
"""

from pathlib import Path
from dataclasses import asdict
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from chanlun.core.xd_calculator import (
    XdCalculator,
    _CandidateExtension,
    _GapConfirmationInvalidated,
)
from chanlun.core.segment_evidence import PendingBoundary


class CompletionCoverageProbe(XdCalculator):
    def _try_completed_reverse_segment(
        self,
        values,
        start,
        direction,
        first,
        result,
        predecessor,
        *,
        latest_witness=None,
    ):
        original = result
        result = super()._try_completed_reverse_segment(
            values,
            start,
            direction,
            first,
            result,
            predecessor,
            latest_witness=latest_witness,
        )
        if predecessor is None or isinstance(original, _GapConfirmationInvalidated):
            return result
        if (
            isinstance(original, PendingBoundary)
            and original.reason != "waiting-first-feature"
        ):
            return result
        limit = len(values) - 1 if latest_witness is None else latest_witness
        if isinstance(original, _CandidateExtension):
            limit = min(limit, original.witness_index - 1)
        if isinstance(result, tuple):
            p = self._proofs[(start, result[0], direction)]
            if p.second_sequence or p.parent_key is not None:
                return result
            limit = min(limit, p.witness_index - 1)
        before = dict(self._proofs)
        candidate = self._try_established_reverse_break(
            values, start, direction, first, predecessor
        )
        candidate_proof = (
            self._proofs.get((start, candidate[0], direction))
            if isinstance(candidate, tuple)
            else None
        )
        self._proofs = before
        if candidate_proof is not None and candidate_proof.witness_index <= limit:
            self._proofs[candidate_proof.key] = candidate_proof
            return candidate
        return result


if __name__ == "__main__":
    import pandas as pd
    from chanlun.core.cl import CL
    from chanlun.cl_utils.price_metadata import (
        strict_snapshot_price_metadata,
        strict_cl_config,
    )
    from script.review_segment_causal_prefixes import read
    from script.review_segment_raw_inputs import raw_geometry
    from script.check_segment_model import check_evidence
    from script.audit_all_segment_results import write_json

    out = ROOT / "output/segment_normalized_reaudit_20260921"
    d = next(
        d
        for d in read(out / "inventory.json")["datasets"]
        if d["id"] == "14beaff7c352076c48167a0e"
    )
    source = read(d["file"])["source_input"]
    frame = pd.read_parquet(source["file"])
    meta = strict_snapshot_price_metadata(frame)
    results = []
    for klass in [XdCalculator, CompletionCoverageProbe]:
        c = CL(
            d["code"],
            d["frequency"],
            strict_cl_config(
                structure_price_quantum=meta.structure_price_quantum,
                price_basis_revision=meta.price_basis_revision,
            ),
            market=d["market"],
        )
        c.xd_calculator = klass()
        c.process_klines(frame, last_bar_closed=True)
        check_evidence(c.xd_calculator, c.get_contiguous_bis())
        rows = [
            {
                "geometry": list(raw_geometry(s, str(meta.structure_price_quantum))),
                "start_pen": s.start_line.index,
                "end_pen": s.end_line.index,
                "confirmed_at": int(s.locked_at.timestamp()) if s.locked_at else None,
                "proof": asdict(s.construction_evidence)
                if s.construction_evidence
                else None,
            }
            for s in c.get_xds()
        ]
        results.append({"variant": klass.__name__, "rows": rows})
        print(klass.__name__, [r for r in rows if r["start_pen"] >= 149], flush=True)
    write_json(
        out / "SH600279_completion_probe.json",
        {"dataset": d, "source": source, "results": results},
    )
