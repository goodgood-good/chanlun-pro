import hashlib
import pytest
from chanlun.screening import evidence
from tests.web.test_warning_lifecycle import frozen_warning


def test_catalogs_share_verified_decoding_but_full_frame_reads_remain_independent(
    tmp_path, monkeypatch
):
    event, _, _ = frozen_warning(tmp_path)
    manifest = event["evidence"]
    directory = tmp_path / event["evidence_run_id"]
    packed = evidence.evidence_paths(directory, event["code"], event["frequency"])[1]
    stat = packed.stat()
    args = (
        str(packed),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        manifest["snapshot_sha256"],
    )
    for method in (
        evidence._catalog_snapshot,
        evidence._selection_catalog,
        evidence._nested_snapshot,
    ):
        method.cache_clear()
    calls = []
    original = evidence._decode_snapshot

    def decode(raw):
        calls.append(hashlib.sha256(raw).hexdigest())
        return original(raw)

    monkeypatch.setattr(evidence, "_decode_snapshot", decode)
    evidence._nested_snapshot(*args)
    evidence._selection_catalog(*args)
    assert calls == [manifest["snapshot_sha256"]]
    evidence.load_evidence(directory, event["code"], event["frequency"], manifest)
    assert len(calls) == 2, (
        "actual frame reads still verify their own bytes and decode independently"
    )
    with pytest.raises(ValueError):
        evidence._nested_snapshot(*args[:-1], "0" * 64)
