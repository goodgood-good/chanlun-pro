from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools import backtest_qmt_fixed_year


def test_qmt_backtest_rejects_an_implicit_full_market_run() -> None:
    with pytest.raises(ValueError, match="bounded research scope required"):
        backtest_qmt_fixed_year.main([])


def test_qmt_backtest_rejects_conflicting_full_market_scope() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        backtest_qmt_fixed_year.main(["--full-market", "--codes", "SH.600000"])


def test_qmt_backtest_has_no_limit_only_full_catalog_path() -> None:
    with pytest.raises(SystemExit):
        backtest_qmt_fixed_year.parser().parse_args(["--limit", "20"])


def test_qmt_backtest_requires_independent_full_market_confirmation() -> None:
    with pytest.raises(ValueError, match="also requires --confirm-large-scope"):
        backtest_qmt_fixed_year.main(["--full-market"])

    with pytest.raises(ValueError, match="profile-scoped --pit-snapshot"):
        backtest_qmt_fixed_year.main(["--full-market", "--confirm-large-scope"])


def test_qmt_backtest_gates_the_actual_selected_scope_above_20() -> None:
    backtest_qmt_fixed_year._validate_scope_authorization(
        full_market=False,
        confirm_large_scope=False,
        selected_count=20,
    )
    backtest_qmt_fixed_year._validate_scope_authorization(
        full_market=False,
        confirm_large_scope=True,
        selected_count=21,
    )

    with pytest.raises(ValueError, match="actual scope contains 21 symbols"):
        backtest_qmt_fixed_year._validate_scope_authorization(
            full_market=False,
            confirm_large_scope=False,
            selected_count=21,
        )


def test_qmt_entrypoint_applies_the_gate_after_scope_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    pit_path = tmp_path / "pit.json"
    pit_path.write_text("{}", encoding="utf-8")
    output_path = tmp_path / "facts"
    snapshot = SimpleNamespace(
        source_start=backtest_qmt_fixed_year.DEFAULT_WARMUP_START,
        source_end=backtest_qmt_fixed_year.DEFAULT_END,
    )
    catalog = tuple((f"SH.{600000 + offset:06d}", "sector") for offset in range(30))
    monkeypatch.setattr(backtest_qmt_fixed_year, "load_snapshot", lambda _: snapshot)
    monkeypatch.setattr(backtest_qmt_fixed_year, "PITMetadataIndex", lambda _: object())
    monkeypatch.setattr(
        backtest_qmt_fixed_year,
        "_catalog_scope",
        lambda *_args, **_kwargs: (catalog, {}),
    )

    with pytest.raises(ValueError, match="actual scope contains 21 symbols"):
        backtest_qmt_fixed_year.main(
            [
                "--codes",
                ",".join(f"SH.{600000 + offset:06d}" for offset in range(21)),
                "--pit-snapshot",
                str(pit_path),
                "--output-dir",
                str(output_path),
            ]
        )

    assert not output_path.exists()


def test_qmt_backtest_requires_profile_scoped_pit_snapshot() -> None:
    with pytest.raises(ValueError, match="profile-scoped --pit-snapshot"):
        backtest_qmt_fixed_year.main(["--codes", "SH.600001"])
    with pytest.raises(ValueError, match="profile-scoped --output-dir"):
        backtest_qmt_fixed_year.main(
            [
                "--codes",
                "SH.600001",
                "--pit-snapshot",
                "pit.json",
            ]
        )


@pytest.mark.parametrize("raw", ("", "all", "*", "SH.600001,"))
def test_qmt_backtest_rejects_scope_aliases_before_loading_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    raw: str,
) -> None:
    monkeypatch.setattr(
        backtest_qmt_fixed_year,
        "load_snapshot",
        lambda _path: pytest.fail("invalid bounded scope must fail before PIT load"),
    )
    with pytest.raises(ValueError, match="codes|bounded research scope"):
        backtest_qmt_fixed_year.main(
            [
                "--codes",
                raw,
                "--pit-snapshot",
                str(tmp_path / "pit.json"),
                "--output-dir",
                str(tmp_path / "facts"),
            ]
        )
