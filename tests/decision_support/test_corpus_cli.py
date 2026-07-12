from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.build_decision_corpus import main
from tests.decision_support.test_corpus_integrity import _VALID_JPEG


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_complete_corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    archive_root = output_root / "archive"
    image_root = archive_root / "articles" / "images" / "01"
    lesson_root.mkdir()
    image_root.mkdir(parents=True)

    lesson_name = "L061_s072_区间套定位标准图解.md"
    (lesson_root / lesson_name).write_text(
        "# 教你炒股票 61：区间套定位标准图解\n\n区间套原文。",
        encoding="utf-8",
    )
    (lesson_root / "_index.md").write_text(
        f"| 72 | 61 | 4 | 0 | {lesson_name} | 预览 |\n",
        encoding="utf-8",
    )
    (archive_root / "articles" / "01_买卖点图解.md").write_text(
        "# 买卖点图解\n\n"
        "- Author: 整理者\n"
        "- Source: https://example.invalid/article\n\n"
        "图前说明。\n\n"
        "![](images/01/01.jpg)\n\n"
        "图后说明。\n",
        encoding="utf-8",
    )
    (image_root / "01.jpg").write_bytes(_VALID_JPEG)
    return lesson_root, output_root, archive_root


def test_cli_returns_two_when_zero_byte_files_exist(tmp_path: Path) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    output_root.mkdir()
    broken = output_root / "broken.jpg"
    broken.write_bytes(b"")

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
        ]
    )

    assert code == 2
    report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert report["summary"] == {
        "invalid": 1,
        "scanned": 1,
        "trusted": 0,
    }
    assert "zero_byte" in {item["code"] for item in report["issues"]}
    zero_issue = next(
        item for item in report["issues"] if item["code"] == "zero_byte"
    )
    assert Path(zero_issue["path"]) == broken.resolve()
    assert broken.read_bytes() == b""
    assert not (build_root / "trusted_manifest.json").exists()

def test_cli_builds_complete_trusted_corpus_with_verifiable_hashes(
    tmp_path: Path,
) -> None:
    lesson_root, output_root, archive_root = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
        ]
    )

    assert code == 0
    expected_outputs = {
        "corpus_units.json",
        "integrity_report.json",
        "repair_report.json",
        "trusted_manifest.json",
    }
    assert {path.name for path in build_root.iterdir()} == expected_outputs

    manifest = json.loads(
        (build_root / "trusted_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["corpus_status"]["integrity"] == "complete"
    assert manifest["corpus_status"]["original_evidence"] == "available"
    assert manifest["corpus_status"]["trusted_images"] == 1
    assert manifest["corpus_status"]["trusted_units"] >= 2

    roots = {
        "lesson_original": lesson_root,
        "lesson_chart": lesson_root,
        "secondary_annotation": output_root,
    }
    for unit in manifest["units"]:
        source = roots[unit["source_tier"]] / unit["source_path"]
        assert source.is_file()
        assert unit["sha256"] == _sha256(source)
    for image in manifest["images"]:
        source = roots[image["source_tier"]] / image["source_path"]
        assert source.is_file()
        assert image["sha256"] == _sha256(source)

    units_payload = json.loads(
        (build_root / "corpus_units.json").read_text(encoding="utf-8")
    )
    assert units_payload["schema_version"] == 1
    assert units_payload["indexed_units"] == len(manifest["units"])
    assert units_payload["units"] == manifest["units"]
    repair_report = json.loads(
        (build_root / "repair_report.json").read_text(encoding="utf-8")
    )
    assert repair_report["summary"] == {"attempted": 0, "failed": 0, "repaired": 0}

def test_cli_repair_returns_three_and_classifies_missing_source_url(
    tmp_path: Path,
) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    output_root.mkdir()
    broken = output_root / "broken.jpg"
    broken.write_bytes(b"")

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
            "--repair",
        ]
    )

    assert code == 3
    repair_report = json.loads(
        (build_root / "repair_report.json").read_text(encoding="utf-8")
    )
    assert repair_report["summary"] == {"attempted": 1, "failed": 1, "repaired": 0}
    assert repair_report["results"][0]["code"] == "missing_source_url"
    assert repair_report["results"][0]["destination"] == "output/broken.jpg"
    assert broken.read_bytes() == b""
    integrity_report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert integrity_report["summary"]["invalid"] == 1

def test_cli_repairs_from_bounded_url_map_and_builds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chanlun.decision_support import corpus_repair

    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    article_root = output_root / "archive" / "articles"
    image_root = article_root / "images" / "01"
    image_root.mkdir(parents=True)
    (article_root / "article.md").write_text(
        "# Article ![](images/01/broken.jpg)",
        encoding="utf-8",
    )
    broken = image_root / "broken.jpg"
    broken.write_bytes(b"")
    url_map = tmp_path / "url-map.json"
    url_map.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "targets": [
                    {
                        "root": "output",
                        "path": "archive/articles/images/01/broken.jpg",
                        "url": "https://example.invalid/broken.jpg",
                        "sha256": hashlib.sha256(_VALID_JPEG).hexdigest(),
                        "media_type": "image/jpeg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return _VALID_JPEG

    monkeypatch.setattr(corpus_repair, "RequestsFetcher", lambda: fetch)

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
            "--repair",
            "--url-map",
            str(url_map),
        ]
    )

    assert code == 0
    assert fetched == ["https://example.invalid/broken.jpg"]
    assert broken.read_bytes() == _VALID_JPEG
    repair_report = json.loads(
        (build_root / "repair_report.json").read_text(encoding="utf-8")
    )
    assert repair_report["summary"] == {"attempted": 1, "failed": 0, "repaired": 1}
    assert repair_report["results"][0]["code"] == "repaired"
    integrity_report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert integrity_report["summary"]["invalid"] == 0
    manifest = json.loads(
        (build_root / "trusted_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["corpus_status"]["original_evidence"] == "missing_original"

def test_cli_rejects_build_root_inside_corpus_root(tmp_path: Path) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    lesson_root.mkdir()
    output_root.mkdir()

    with pytest.raises(ValueError, match="build root must be outside corpus roots"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(output_root / "build"),
            ]
        )


def test_cli_rejects_missing_corpus_root(tmp_path: Path) -> None:
    lesson_root = tmp_path / "lessons"
    lesson_root.mkdir()

    with pytest.raises(ValueError, match="corpus root must be an existing directory"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(tmp_path / "missing-output"),
                "--build-root",
                str(tmp_path / "build"),
            ]
        )


def test_cli_clears_stale_outputs_when_corpus_root_is_missing(
    tmp_path: Path,
) -> None:
    lesson_root = tmp_path / "lessons"
    lesson_root.mkdir()
    build_root = tmp_path / "build"
    build_root.mkdir()
    output_names = (
        "trusted_manifest.json",
        "corpus_units.json",
        "integrity_report.json",
        "repair_report.json",
    )
    for name in output_names:
        (build_root / name).write_bytes(b"stale")

    with pytest.raises(ValueError, match="corpus root must be an existing directory"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(tmp_path / "missing-output"),
                "--build-root",
                str(build_root),
            ]
        )

    assert all(not (build_root / name).exists() for name in output_names)


def test_cli_reads_secret_source_url_from_environment_without_reporting_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chanlun.decision_support import corpus_repair

    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    article_root = output_root / "archive" / "articles"
    image_root = article_root / "images" / "01"
    image_root.mkdir(parents=True)
    (article_root / "article.md").write_text(
        "# Article ![](images/01/secret.jpg)",
        encoding="utf-8",
    )
    (image_root / "secret.jpg").write_bytes(b"")
    url_map = tmp_path / "url-map.json"
    url_map.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "targets": [
                    {
                        "root": "output",
                        "path": "archive/articles/images/01/secret.jpg",
                        "url_env": "CHANLUN_TEST_SIGNED_URL",
                        "sha256": hashlib.sha256(_VALID_JPEG).hexdigest(),
                        "media_type": "image/jpeg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    secret_url = "https://example.invalid/secret.jpg?token=super-secret"
    monkeypatch.setenv("CHANLUN_TEST_SIGNED_URL", secret_url)
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return _VALID_JPEG

    monkeypatch.setattr(corpus_repair, "RequestsFetcher", lambda: fetch)

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
            "--repair",
            "--url-map",
            str(url_map),
        ]
    )

    assert code == 0
    assert fetched == [secret_url]
    report_text = (build_root / "repair_report.json").read_text(encoding="utf-8")
    assert "super-secret" not in report_text
    repair_report = json.loads(report_text)
    assert repair_report["results"][0]["source_url"] == (
        "https://example.invalid/secret.jpg"
    )

def test_cli_success_outputs_are_byte_deterministic(tmp_path: Path) -> None:
    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    first = tmp_path / "build-a"
    second = tmp_path / "build-b"
    arguments = [
        "--lesson-root",
        str(lesson_root),
        "--output-root",
        str(output_root),
    ]

    assert main([*arguments, "--build-root", str(first)]) == 0
    assert main([*arguments, "--build-root", str(second)]) == 0

    for name in (
        "corpus_units.json",
        "integrity_report.json",
        "repair_report.json",
        "trusted_manifest.json",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_cli_rejects_url_map_path_traversal_without_mutation(tmp_path: Path) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    output_root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    url_map = tmp_path / "url-map.json"
    url_map.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "targets": [
                    {
                        "root": "output",
                        "path": "../outside.jpg",
                        "url": "https://example.invalid/outside.jpg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe url map path"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(build_root),
                "--repair",
                "--url-map",
                str(url_map),
            ]
        )

    assert outside.read_bytes() == b"outside"
    assert not build_root.exists()


def test_cli_replaces_stale_trust_with_verified_partial_after_failure(
    tmp_path: Path,
) -> None:
    lesson_root, output_root, archive_root = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"
    arguments = [
        "--lesson-root",
        str(lesson_root),
        "--output-root",
        str(output_root),
        "--build-root",
        str(build_root),
    ]
    assert main(arguments) == 0
    assert (build_root / "trusted_manifest.json").is_file()

    (archive_root / "articles" / "images" / "01" / "01.jpg").write_bytes(b"")
    assert main(arguments) == 2

    manifest = json.loads(
        (build_root / "trusted_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["corpus_status"]["integrity"] == "incomplete"
    assert manifest["corpus_status"]["trusted_images"] == 0
    assert manifest["corpus_status"]["trusted_units"] >= 2
    assert (build_root / "corpus_units.json").is_file()
    report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert report["summary"]["invalid"] == 1

def test_cli_clears_stale_trust_before_url_map_validation(tmp_path: Path) -> None:
    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"
    arguments = [
        "--lesson-root",
        str(lesson_root),
        "--output-root",
        str(output_root),
        "--build-root",
        str(build_root),
    ]
    assert main(arguments) == 0
    assert (build_root / "trusted_manifest.json").is_file()
    url_map = tmp_path / "unsafe-map.json"
    url_map.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "targets": [
                    {
                        "root": "output",
                        "path": "../outside.jpg",
                        "url": "https://example.invalid/outside.jpg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe url map path"):
        main([*arguments, "--repair", "--url-map", str(url_map)])

    assert not (build_root / "trusted_manifest.json").exists()
    assert not (build_root / "corpus_units.json").exists()


def test_cli_removes_partial_manifest_when_units_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import build_decision_corpus as cli

    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"
    real_write = cli._atomic_write_json

    def fail_units(path: Path, payload: object) -> Path:
        if Path(path).name == "corpus_units.json":
            raise OSError("injected units write failure")
        return real_write(path, payload)

    monkeypatch.setattr(cli, "_atomic_write_json", fail_units)

    with pytest.raises(OSError, match="injected units write failure"):
        cli.main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(build_root),
            ]
        )

    assert not (build_root / "trusted_manifest.json").exists()
    assert not (build_root / "corpus_units.json").exists()

def test_cli_bounds_missing_url_map_as_validation_error(tmp_path: Path) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    lesson_root.mkdir()
    output_root.mkdir()

    with pytest.raises(ValueError, match="invalid url map"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(tmp_path / "build"),
                "--repair",
                "--url-map",
                str(tmp_path / "missing-map.json"),
            ]
        )

def test_cli_repair_report_namespaces_same_relative_path_across_roots(
    tmp_path: Path,
) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    output_root.mkdir()
    (lesson_root / "broken.jpg").write_bytes(b"")
    (output_root / "broken.jpg").write_bytes(b"")

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
            "--repair",
        ]
    )

    assert code == 3
    report = json.loads(
        (build_root / "repair_report.json").read_text(encoding="utf-8")
    )
    assert [item["destination"] for item in report["results"]] == [
        "lesson/broken.jpg",
        "output/broken.jpg",
    ]
    assert all(item["code"] == "missing_source_url" for item in report["results"])

def test_cli_aborts_when_corpus_changes_between_parse_and_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import build_decision_corpus as cli

    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    lesson_path = lesson_root / "L061_s072_区间套定位标准图解.md"
    build_root = tmp_path / "build"
    real_build = cli.CorpusIndex.build

    def mutate_then_build(units):
        index = real_build(units)
        lesson_path.write_text(
            lesson_path.read_text(encoding="utf-8") + "\n\n扫描后变化。",
            encoding="utf-8",
        )
        return index

    monkeypatch.setattr(cli.CorpusIndex, "build", mutate_then_build)

    with pytest.raises(RuntimeError, match="corpus changed during build"):
        cli.main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(build_root),
            ]
        )

    assert not (build_root / "trusted_manifest.json").exists()
    assert not (build_root / "corpus_units.json").exists()

def test_cli_rejects_url_map_target_that_is_not_invalid(tmp_path: Path) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    output_root.mkdir()
    (output_root / "valid.jpg").write_bytes(_VALID_JPEG)
    url_map = tmp_path / "url-map.json"
    url_map.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "targets": [
                    {
                        "root": "output",
                        "path": "valid.jpg",
                        "url": "https://example.invalid/valid.jpg",
                        "sha256": hashlib.sha256(_VALID_JPEG).hexdigest(),
                        "media_type": "image/jpeg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="url map target is not an invalid corpus file"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(build_root),
                "--repair",
                "--url-map",
                str(url_map),
            ]
        )

    assert not (build_root / "trusted_manifest.json").exists()

def test_cli_quarantines_invalid_files_and_builds_verified_partial_manifest(
    tmp_path: Path,
) -> None:
    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"
    (output_root / "broken.jpg").write_bytes(b"")

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
        ]
    )

    assert code == 2
    manifest = json.loads(
        (build_root / "trusted_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["corpus_status"]["integrity"] == "incomplete"
    assert manifest["corpus_status"]["original_evidence"] == "available"
    assert manifest["corpus_status"]["trusted_units"] >= 2
    assert manifest["corpus_status"]["trusted_images"] == 1
    assert all(item["source_path"] != "broken.jpg" for item in manifest["images"])
    units = json.loads(
        (build_root / "corpus_units.json").read_text(encoding="utf-8")
    )
    assert units["indexed_units"] == len(manifest["units"])
    report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert report["summary"]["invalid"] == 1

def test_cli_marks_missing_markdown_image_reference_incomplete(
    tmp_path: Path,
) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    article_root = output_root / "archive" / "articles"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    article_root.mkdir(parents=True)
    (article_root / "article.md").write_text(
        """# Article

Before

![](images/missing.jpg)

After
""",
        encoding="utf-8",
    )

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
        ]
    )

    assert code == 2
    report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert "missing_reference" in {item["code"] for item in report["issues"]}
    manifest = json.loads(
        (build_root / "trusted_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["corpus_status"]["integrity"] == "incomplete"
    assert manifest["corpus_status"]["trusted_units"] >= 1
    assert manifest["corpus_status"]["trusted_images"] == 0


def test_cli_rejects_forged_archive_index_counts(tmp_path: Path) -> None:
    lesson_root, output_root, archive_root = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"
    (archive_root / "index.json").write_text(
        json.dumps(
            {
                "count": 99,
                "totalImages": 99,
                "downloadedImages": 99,
                "imageErrors": 0,
                "articles": [],
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
        ]
    )

    assert code == 2
    report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert "archive_count_mismatch" in {
        item["code"] for item in report["issues"]
    }
    manifest = json.loads(
        (build_root / "trusted_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["corpus_status"]["integrity"] == "incomplete"


def test_cli_rejects_archive_index_without_articles(tmp_path: Path) -> None:
    lesson_root, output_root, archive_root = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"
    (archive_root / "index.json").write_text(
        json.dumps(
            {
                "count": 999,
                "totalImages": 999,
                "downloadedImages": 999,
                "imageErrors": 0,
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
        ]
    )

    assert code == 2
    report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert "invalid_archive_index" in {
        item["code"] for item in report["issues"]
    }
    manifest = json.loads(
        (build_root / "trusted_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["corpus_status"]["integrity"] == "incomplete"


def test_cli_empty_roots_do_not_write_trusted_outputs(tmp_path: Path) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    output_root.mkdir()

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
        ]
    )

    assert code == 2
    assert not (build_root / "trusted_manifest.json").exists()
    assert not (build_root / "corpus_units.json").exists()
    report = json.loads(
        (build_root / "integrity_report.json").read_text(encoding="utf-8")
    )
    assert "empty_trusted_corpus" in {item["code"] for item in report["issues"]}


def test_cli_rejects_corpus_roots_inside_build_root_without_overwrite(
    tmp_path: Path,
) -> None:
    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    manifest_sentinel = tmp_path / "trusted_manifest.json"
    units_sentinel = tmp_path / "corpus_units.json"
    manifest_sentinel.write_bytes(b"manifest-sentinel")
    units_sentinel.write_bytes(b"units-sentinel")

    with pytest.raises(ValueError, match="build root must not contain corpus roots"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(tmp_path),
            ]
        )

    assert manifest_sentinel.read_bytes() == b"manifest-sentinel"
    assert units_sentinel.read_bytes() == b"units-sentinel"


def test_cli_aborts_when_corpus_changes_after_manifest_write(
    tmp_path: Path, monkeypatch
) -> None:
    from tools import build_decision_corpus as cli

    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    lesson_path = next(lesson_root.glob("L*.md"))
    build_root = tmp_path / "build"
    real_write = cli.write_trusted_manifest

    def write_then_mutate(*args, **kwargs):
        result = real_write(*args, **kwargs)
        lesson_path.write_text(
            lesson_path.read_text(encoding="utf-8") + """

changed after manifest
""",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(cli, "write_trusted_manifest", write_then_mutate)

    with pytest.raises(RuntimeError, match="corpus changed during build"):
        cli.main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(build_root),
            ]
        )

    assert not (build_root / "trusted_manifest.json").exists()
    assert not (build_root / "corpus_units.json").exists()


def test_cli_repairs_missing_reference_from_archive_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    from chanlun.decision_support import corpus_repair

    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    archive_root = output_root / "archive"
    article_root = archive_root / "articles"
    image_path = article_root / "images" / "missing.jpg"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    article_root.mkdir(parents=True)
    (article_root / "article.md").write_text(
        """# Article

Before

![](images/missing.jpg)

After
""",
        encoding="utf-8",
    )
    source_url = "https://example.invalid/missing.jpg"
    (archive_root / "index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repair_targets": [
                    {
                        "path": "articles/images/missing.jpg",
                        "source_url": source_url,
                        "sha256": hashlib.sha256(_VALID_JPEG).hexdigest(),
                        "media_type": "image/jpeg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return _VALID_JPEG

    monkeypatch.setattr(corpus_repair, "RequestsFetcher", lambda: fetch)

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
            "--repair",
        ]
    )

    assert code == 0
    assert fetched == [source_url]
    assert image_path.read_bytes() == _VALID_JPEG
    manifest = json.loads(
        (build_root / "trusted_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["corpus_status"]["integrity"] == "complete"
    assert manifest["corpus_status"]["trusted_images"] == 1


def test_cli_missing_url_environment_is_bounded_repair_failure(
    tmp_path: Path, monkeypatch
) -> None:
    lesson_root = tmp_path / "lessons"
    output_root = tmp_path / "output"
    build_root = tmp_path / "build"
    lesson_root.mkdir()
    output_root.mkdir()
    (output_root / "broken.jpg").write_bytes(b"")
    url_map = tmp_path / "url-map.json"
    url_map.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "targets": [
                    {
                        "root": "output",
                        "path": "broken.jpg",
                        "url_env": "CHANLUN_TEST_MISSING_URL",
                        "media_type": "image/jpeg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("CHANLUN_TEST_MISSING_URL", raising=False)

    code = main(
        [
            "--lesson-root",
            str(lesson_root),
            "--output-root",
            str(output_root),
            "--build-root",
            str(build_root),
            "--repair",
            "--url-map",
            str(url_map),
        ]
    )

    assert code == 3
    repair_report = json.loads(
        (build_root / "repair_report.json").read_text(encoding="utf-8")
    )
    assert repair_report["results"][0]["code"] == "missing_source_url"
    assert not (build_root / "trusted_manifest.json").exists()


def test_cli_bounds_url_map_before_text_decode(
    tmp_path: Path, monkeypatch
) -> None:
    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"
    url_map = tmp_path / "oversized-map.json"
    url_map.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    real_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path.resolve() == url_map.resolve():
            raise AssertionError("url map was decoded before size was bounded")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    with pytest.raises(ValueError, match="url map is too large"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(build_root),
                "--repair",
                "--url-map",
                str(url_map),
            ]
        )


def test_cli_clears_all_stale_outputs_before_validation(tmp_path: Path) -> None:
    lesson_root, output_root, _ = _write_complete_corpus(tmp_path)
    build_root = tmp_path / "build"
    build_root.mkdir()
    output_names = (
        "trusted_manifest.json",
        "corpus_units.json",
        "integrity_report.json",
        "repair_report.json",
    )
    for name in output_names:
        (build_root / name).write_bytes(b"stale")
    url_map = tmp_path / "invalid-map.json"
    url_map.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid url map"):
        main(
            [
                "--lesson-root",
                str(lesson_root),
                "--output-root",
                str(output_root),
                "--build-root",
                str(build_root),
                "--repair",
                "--url-map",
                str(url_map),
            ]
        )

    assert all(not (build_root / name).exists() for name in output_names)


def test_cli_argument_errors_do_not_use_integrity_exit_code() -> None:
    from tools.build_decision_corpus import _parser

    with pytest.raises(ValueError, match="invalid arguments"):
        _parser().parse_args([])
