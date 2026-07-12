from cl_app.services.static_precompress import precompress_directory


def test_precompress_skips_unused_tradingview_bundles(tmp_path):
    charting_library = tmp_path / "charting_library"
    live = charting_library / "bundles" / "live.js"
    unused = charting_library / "bundles_unused" / "old.js"
    live.parent.mkdir(parents=True)
    unused.parent.mkdir(parents=True)
    live.write_text("x" * 2048, encoding="utf-8")
    unused.write_text("x" * 2048, encoding="utf-8")

    compressed, _skipped, _elapsed = precompress_directory(str(charting_library))

    assert compressed == 1
    assert live.with_name("live.js.gz").is_file()
    assert not unused.with_name("old.js.gz").exists()