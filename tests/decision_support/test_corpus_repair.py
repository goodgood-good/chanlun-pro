import hashlib
import json
from pathlib import Path

import pytest

from chanlun.decision_support import corpus_repair
from chanlun.decision_support.corpus_repair import (
    RepairFetchError,
    RequestsFetcher,
    RepairResult,
    RepairTarget,
    repair_targets,
    write_repair_report,
)
from tests.decision_support.test_corpus_integrity import _VALID_JPEG


def test_repair_rejects_destination_outside_allowed_root_without_fetch(tmp_path: Path):
    called = False

    def fetch(url: str) -> bytes:
        nonlocal called
        called = True
        return _VALID_JPEG

    target = RepairTarget(
        url="https://example.invalid/a",
        destination=tmp_path.parent / "escape.jpg",
    )

    result = repair_targets((target,), fetch, tmp_path)[0]

    assert result.ok is False
    assert result.code == "path_outside_root"
    assert called is False


def test_repair_does_not_replace_target_with_empty_response(tmp_path: Path):
    destination = tmp_path / "chart.jpg"
    destination.write_bytes(b"old")
    target = RepairTarget("https://example.invalid/a", destination)

    result = repair_targets((target,), lambda _: b"", tmp_path)[0]

    assert result.ok is False
    assert result.code == "empty_response"
    assert destination.read_bytes() == b"old"


def test_repair_does_not_replace_target_with_invalid_image(tmp_path: Path):
    destination = tmp_path / "chart.jpg"
    destination.write_bytes(b"old")
    target = RepairTarget("https://example.invalid/a", destination)

    result = repair_targets((target,), lambda _: b"not-an-image", tmp_path)[0]

    assert result.ok is False
    assert result.code == "invalid_payload"
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob("*.repair-*.jpg")) == []


def test_repair_atomically_replaces_with_verified_image(tmp_path: Path):
    destination = tmp_path / "chart.jpg"
    destination.write_bytes(b"old")
    expected_hash = hashlib.sha256(_VALID_JPEG).hexdigest()
    target = RepairTarget(
        "https://example.invalid/a",
        destination,
        expected_sha256=expected_hash,
        expected_media_type="image/jpeg",
    )

    result = repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)[0]

    assert result.ok is True
    assert result.code == "repaired"
    assert result.bytes_written == len(_VALID_JPEG)
    assert result.sha256 == expected_hash
    assert destination.read_bytes() == _VALID_JPEG
    assert list(tmp_path.glob("*.repair-*.jpg")) == []


def test_repair_hash_mismatch_retains_existing_target(tmp_path: Path):
    destination = tmp_path / "chart.jpg"
    destination.write_bytes(b"old")
    target = RepairTarget(
        "https://example.invalid/a",
        destination,
        expected_sha256="0" * 64,
    )

    result = repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)[0]

    assert result.ok is False
    assert result.code == "sha256_mismatch"
    assert destination.read_bytes() == b"old"


def test_repair_reports_missing_source_url_without_fetch(tmp_path: Path):
    called = False

    def fetch(url: str) -> bytes:
        nonlocal called
        called = True
        return b"unused"

    result = repair_targets((RepairTarget("", tmp_path / "missing.jpg"),), fetch, tmp_path)[0]

    assert result.code == "missing_source_url"
    assert result.ok is False
    assert called is False


def test_repair_preserves_authentication_failure_code(tmp_path: Path):
    def fetch(url: str) -> bytes:
        raise RepairFetchError("authentication_required")

    result = repair_targets(
        (RepairTarget("https://example.invalid/private", tmp_path / "private.jpg"),),
        fetch,
        tmp_path,
    )[0]

    assert result.ok is False
    assert result.code == "authentication_required"


def test_repair_requires_semantic_verifier_for_unpinned_text(tmp_path: Path):
    destination = tmp_path / "article.md"
    destination.write_text("old", encoding="utf-8")
    target = RepairTarget("https://example.invalid/article", destination)

    result = repair_targets((target,), lambda _: b"# article\n\nbody", tmp_path)[0]

    assert result.ok is False
    assert result.code == "semantic_verification_required"
    assert destination.read_text("utf-8") == "old"


def test_repair_accepts_text_when_semantic_verifier_approves(tmp_path: Path):
    destination = tmp_path / "article.md"
    target = RepairTarget("https://example.invalid/article", destination)
    calls = []

    def verify(target, corpus_file, payload):
        calls.append((target.url, corpus_file.media_type, payload))
        return None

    result = repair_targets(
        (target,),
        lambda _: b"# article\n\nbody",
        tmp_path,
        verify=verify,
    )[0]

    assert result.ok is True
    assert destination.read_bytes() == b"# article\n\nbody"
    assert calls == [(target.url, "text/markdown", b"# article\n\nbody")]


def test_verifier_exception_is_bounded_and_next_target_continues(tmp_path: Path):
    first = RepairTarget("https://example.invalid/article", tmp_path / "article.md")
    second = RepairTarget("", tmp_path / "missing.jpg")

    def verify(target, corpus_file, payload):
        raise RuntimeError("private verifier detail")

    results = repair_targets(
        (first, second),
        lambda _: b"# article\n\nbody",
        tmp_path,
        verify=verify,
    )

    assert [result.code for result in results] == [
        "semantic_verification_failed",
        "missing_source_url",
    ]
    assert not first.destination.exists()


def test_repair_rechecks_temporary_bytes_after_verifier(tmp_path: Path):
    destination = tmp_path / "article.md"
    destination.write_text("old", encoding="utf-8")
    target = RepairTarget("https://example.invalid/article", destination)

    def verify(target, corpus_file, payload):
        corpus_file.path.write_bytes(b"tampered after verification")
        return None

    result = repair_targets(
        (target,),
        lambda _: b"# article\n\nbody",
        tmp_path,
        verify=verify,
    )[0]

    assert result.ok is False
    assert result.code == "payload_changed_before_replace"
    assert destination.read_text("utf-8") == "old"


class _FakeResponse:
    def __init__(self, status_code=200, chunks=(), content_length=None):
        self.status_code = status_code
        self._chunks = tuple(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.url = "https://example.invalid/final"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def iter_content(self, chunk_size):
        yield from self._chunks


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_requests_fetcher_uses_timeout_streaming_and_size_bound():
    session = _FakeSession(_FakeResponse(chunks=(b"ab", b"cd"), content_length=4))
    fetch = RequestsFetcher(
        session=session,
        connect_timeout=1.5,
        read_timeout=4.0,
        max_response_bytes=4,
    )

    payload = fetch("https://example.invalid/a")

    assert payload == b"abcd"
    assert session.calls == [
        (
            "https://example.invalid/a",
            {"allow_redirects": True, "stream": True, "timeout": (1.5, 4.0)},
        )
    ]


def test_requests_fetcher_maps_authentication_and_oversize_safely():
    denied = RequestsFetcher(session=_FakeSession(_FakeResponse(status_code=403)))
    with pytest.raises(RepairFetchError) as auth_error:
        denied("https://example.invalid/private")
    assert auth_error.value.code == "authentication_required"

    oversized = RequestsFetcher(
        session=_FakeSession(_FakeResponse(chunks=(b"abc",), content_length=3)),
        max_response_bytes=2,
    )
    with pytest.raises(RepairFetchError) as size_error:
        oversized("https://example.invalid/large")
    assert size_error.value.code == "response_too_large"


def test_repair_targets_uses_requests_fetcher_when_fetch_is_none(tmp_path: Path, monkeypatch):
    calls = []

    class BoundedFetcher:
        def __call__(self, url):
            calls.append(url)
            return _VALID_JPEG

    monkeypatch.setattr(corpus_repair, "RequestsFetcher", BoundedFetcher)
    target = RepairTarget("https://example.invalid/a", tmp_path / "a.jpg")

    result = repair_targets((target,), None, tmp_path)[0]

    assert result.ok is True
    assert calls == [target.url]


def test_malformed_url_is_bounded_and_next_target_continues(tmp_path: Path):
    results = repair_targets(
        (
            RepairTarget("https://[", tmp_path / "bad.jpg"),
            RepairTarget("", tmp_path / "missing.jpg"),
        ),
        lambda _: _VALID_JPEG,
        tmp_path,
    )

    assert [result.code for result in results] == ["invalid_source_url", "missing_source_url"]


def test_untrusted_external_error_codes_are_normalized(tmp_path: Path):
    def unsafe_fetch(url):
        raise RepairFetchError("Authorization: Bearer secret")

    fetch_result = repair_targets(
        (RepairTarget("https://example.invalid/a", tmp_path / "a.jpg"),),
        unsafe_fetch,
        tmp_path,
    )[0]

    def unsafe_verify(target, corpus_file, payload):
        return "Authorization: Bearer secret"

    verify_result = repair_targets(
        (RepairTarget("https://example.invalid/a", tmp_path / "a.md"),),
        lambda _: b"# article\n\nbody",
        tmp_path,
        verify=unsafe_verify,
    )[0]

    assert fetch_result.code == "fetch_failed"
    assert verify_result.code == "semantic_verification_failed"
    assert "secret" not in repr((fetch_result, verify_result))


def test_write_repair_report_rejects_path_outside_allowed_root(tmp_path: Path):
    outside = tmp_path.parent / "outside-report.json"

    with pytest.raises(ValueError, match="report path outside allowed_root"):
        write_repair_report(outside, (), tmp_path)

    assert not outside.exists()


def test_repair_report_sort_covers_every_public_field(tmp_path: Path):
    first_result = RepairResult("same.jpg", "u", False, "same", 0, "hash")
    second_result = RepairResult("same.jpg", "u", True, "same", 1, "hash")
    first = tmp_path / "first-tie.json"
    second = tmp_path / "second-tie.json"

    write_repair_report(first, (first_result, second_result), tmp_path)
    write_repair_report(second, (second_result, first_result), tmp_path)

    assert first.read_bytes() == second.read_bytes()


def test_write_repair_report_is_atomic_deterministic_and_complete(tmp_path: Path):
    results = (
        RepairResult("b.jpg", "https://example.invalid/b", False, "authentication_required"),
        RepairResult("a.jpg", "", False, "missing_source_url"),
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    write_repair_report(first, results, tmp_path)
    write_repair_report(second, tuple(reversed(results)), tmp_path)

    assert first.read_bytes() == second.read_bytes()
    body = json.loads(first.read_text("utf-8"))
    assert body["schema_version"] == 1
    assert body["summary"] == {"attempted": 2, "failed": 2, "repaired": 0}
    assert [item["destination"] for item in body["results"]] == ["a.jpg", "b.jpg"]
    assert not first.with_suffix(first.suffix + ".tmp").exists()

def test_repair_does_not_overwrite_destination_changed_during_fetch(
    tmp_path: Path,
):
    destination = tmp_path / "chart.jpg"
    destination.write_bytes(b"")
    target = RepairTarget(
        "https://example.invalid/chart.jpg",
        destination,
        expected_sha256=hashlib.sha256(_VALID_JPEG).hexdigest(),
        expected_media_type="image/jpeg",
    )

    def fetch(url: str) -> bytes:
        destination.write_bytes(_VALID_JPEG)
        return _VALID_JPEG

    result = repair_targets((target,), fetch, tmp_path)[0]

    assert result.ok is False
    assert result.code == "destination_changed_before_replace"
    assert destination.read_bytes() == _VALID_JPEG


def test_repair_does_not_overwrite_change_after_final_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "chart.jpg"
    destination.write_bytes(b"")
    target = RepairTarget(
        "https://example.invalid/chart.jpg",
        destination,
        expected_sha256=hashlib.sha256(_VALID_JPEG).hexdigest(),
        expected_media_type="image/jpeg",
    )
    real_snapshot = corpus_repair._destination_snapshot
    destination_snapshots = 0

    def snapshot_then_race(path: Path):
        nonlocal destination_snapshots
        snapshot = real_snapshot(path)
        if Path(path) == destination:
            destination_snapshots += 1
            if destination_snapshots == 2:
                destination.write_bytes(b"concurrent-writer")
        return snapshot

    monkeypatch.setattr(
        corpus_repair,
        "_destination_snapshot",
        snapshot_then_race,
    )

    result = repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)[0]

    assert destination_snapshots >= 2
    assert result.ok is False
    assert result.code == "destination_changed_before_replace"
    assert destination.read_bytes() == b"concurrent-writer"


def test_repair_restores_original_when_install_is_interrupted(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "chart.jpg"
    original = b"original-target"
    destination.write_bytes(original)
    target = RepairTarget(
        "https://example.invalid/chart.jpg",
        destination,
        expected_sha256=hashlib.sha256(_VALID_JPEG).hexdigest(),
        expected_media_type="image/jpeg",
    )
    real_link = corpus_repair.os.link

    def interrupt_payload_install(source, target_path):
        if (
            Path(target_path) == destination
            and ".repair-" in Path(source).name
            and ".repair-backup-" not in Path(source).name
        ):
            raise KeyboardInterrupt("interrupted install")
        return real_link(source, target_path)

    monkeypatch.setattr(corpus_repair.os, "link", interrupt_payload_install)

    with pytest.raises(KeyboardInterrupt, match="interrupted install"):
        repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)

    assert destination.read_bytes() == original
    assert list(tmp_path.glob("*.repair-backup-*.jpg")) == []
    assert list(tmp_path.glob("*.repair-*.jpg")) == []


def test_repair_keeps_backup_when_hardlink_install_fails(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "chart.jpg"
    original = b"original-target"
    destination.write_bytes(original)
    target = RepairTarget(
        "https://example.invalid/chart.jpg",
        destination,
        expected_sha256=hashlib.sha256(_VALID_JPEG).hexdigest(),
        expected_media_type="image/jpeg",
    )

    def fail_hardlinks(source, target_path):
        raise OSError("hardlinks unavailable")

    monkeypatch.setattr(corpus_repair.os, "link", fail_hardlinks)

    result = repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)[0]

    backups = list(tmp_path.glob("*.repair-backup-*.jpg"))
    assert result.ok is False
    assert result.code == "replace_failed"
    assert not destination.exists()
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert [
        path
        for path in tmp_path.glob("*.repair-*.jpg")
        if ".repair-backup-" not in path.name
    ] == []


def test_repair_preserves_both_versions_when_interrupt_follows_payload_link(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "chart.jpg"
    original = b"original-target"
    destination.write_bytes(original)
    target = RepairTarget(
        "https://example.invalid/chart.jpg",
        destination,
        expected_sha256=hashlib.sha256(_VALID_JPEG).hexdigest(),
        expected_media_type="image/jpeg",
    )
    real_link = corpus_repair.os.link

    def link_then_interrupt(source, target_path):
        result = real_link(source, target_path)
        if (
            Path(target_path) == destination
            and ".repair-" in Path(source).name
            and ".repair-backup-" not in Path(source).name
        ):
            raise KeyboardInterrupt("interrupted after link")
        return result

    monkeypatch.setattr(corpus_repair.os, "link", link_then_interrupt)

    with pytest.raises(KeyboardInterrupt, match="interrupted after link"):
        repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)

    backups = list(tmp_path.glob("*.repair-backup-*.jpg"))
    assert destination.read_bytes() == _VALID_JPEG
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert [
        path
        for path in tmp_path.glob("*.repair-*.jpg")
        if ".repair-backup-" not in path.name
    ] == []


def test_repair_rolls_back_when_interrupt_follows_backup_move(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "chart.jpg"
    original = b"original-target"
    destination.write_bytes(original)
    target = RepairTarget(
        "https://example.invalid/chart.jpg",
        destination,
        expected_sha256=hashlib.sha256(_VALID_JPEG).hexdigest(),
        expected_media_type="image/jpeg",
    )
    real_replace = corpus_repair.os.replace

    def move_then_interrupt(source, target_path):
        result = real_replace(source, target_path)
        if (
            Path(source) == destination
            and ".repair-backup-" in Path(target_path).name
        ):
            raise KeyboardInterrupt("interrupted after backup move")
        return result

    monkeypatch.setattr(corpus_repair.os, "replace", move_then_interrupt)

    with pytest.raises(KeyboardInterrupt, match="interrupted after backup move"):
        repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)

    assert destination.read_bytes() == original
    assert list(tmp_path.glob("*.repair-backup-*.jpg")) == []
    assert list(tmp_path.glob("*.repair-*.jpg")) == []


def test_repair_keeps_backup_when_concurrent_target_blocks_install(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "chart.jpg"
    original = b"original-target"
    concurrent = b"concurrent-target"
    destination.write_bytes(original)
    target = RepairTarget(
        "https://example.invalid/chart.jpg",
        destination,
        expected_sha256=hashlib.sha256(_VALID_JPEG).hexdigest(),
        expected_media_type="image/jpeg",
    )
    real_link = corpus_repair.os.link

    def concurrent_target_before_payload_link(source, target_path):
        if (
            Path(target_path) == destination
            and ".repair-" in Path(source).name
            and ".repair-backup-" not in Path(source).name
        ):
            destination.write_bytes(concurrent)
            raise FileExistsError("concurrent target won")
        return real_link(source, target_path)

    monkeypatch.setattr(
        corpus_repair.os,
        "link",
        concurrent_target_before_payload_link,
    )

    result = repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)[0]

    backups = list(tmp_path.glob("*.repair-backup-*.jpg"))
    assert result.ok is False
    assert result.code == "destination_changed_before_replace"
    assert destination.read_bytes() == concurrent
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_repair_never_unlinks_concurrent_target_during_rollback(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "chart.jpg"
    original = b"original-target"
    concurrent = b"concurrent-target"
    destination.write_bytes(original)
    target = RepairTarget(
        "https://example.invalid/chart.jpg",
        destination,
        expected_sha256=hashlib.sha256(_VALID_JPEG).hexdigest(),
        expected_media_type="image/jpeg",
    )
    real_link = corpus_repair.os.link

    def replace_payload_then_interrupt(source, target_path):
        result = real_link(source, target_path)
        if (
            Path(target_path) == destination
            and ".repair-" in Path(source).name
            and ".repair-backup-" not in Path(source).name
        ):
            destination.unlink()
            destination.write_bytes(concurrent)
            raise KeyboardInterrupt("interrupted after concurrent replace")
        return result

    monkeypatch.setattr(corpus_repair.os, "link", replace_payload_then_interrupt)

    with pytest.raises(KeyboardInterrupt, match="interrupted after concurrent replace"):
        repair_targets((target,), lambda _: _VALID_JPEG, tmp_path)

    backups = list(tmp_path.glob("*.repair-backup-*.jpg"))
    assert destination.read_bytes() == concurrent
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
