from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
from pathlib import Path
import random

import pytest

from chanlun.decision_support import fingerprints as subject


class _State(Enum):
    ACTIVE = "启用"


@dataclass(frozen=True)
class _Evidence:
    symbol: str
    captured_at: datetime
    optional_note: str | None = field(
        default=None,
        metadata={"canonical_omit_if_none": True},
    )


def _legacy_sha256_json(value: object) -> str:
    encoded = subject.canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        -17,
        1.0,
        -0.0,
        1.25,
        '中文 "quote" \\ slash',
        Decimal("-120.3400"),
        datetime(2026, 8, 26, 9, 31, 2, tzinfo=timezone.utc),
        Path("evidence") / "事实.json",
        _State.ACTIVE,
        ("SH.600000", [1, 2.0, {"z": False, "a": None}]),
        _Evidence(
            symbol="SH.600000",
            captured_at=datetime(
                2026,
                8,
                26,
                9,
                31,
                2,
                tzinfo=timezone.utc,
            ),
        ),
    ],
)
def test_streaming_sha256_matches_legacy_canonical_json(value: object) -> None:
    assert subject.sha256_json(value) == _legacy_sha256_json(value)


def test_streaming_sha256_matches_random_nested_legacy_documents() -> None:
    generator = random.Random(20260826)

    def nested(depth: int) -> object:
        primitives: tuple[object, ...] = (
            None,
            False,
            True,
            -1,
            0,
            12.5,
            "",
            '中文\n"证据"',
        )
        if depth == 0:
            return generator.choice(primitives)
        kind = generator.randrange(4)
        if kind == 0:
            return generator.choice(primitives)
        if kind == 1:
            return [nested(depth - 1) for _ in range(generator.randrange(5))]
        if kind == 2:
            return tuple(
                nested(depth - 1) for _ in range(generator.randrange(5))
            )
        return {
            f"键-{index}-{generator.randrange(1000)}": nested(depth - 1)
            for index in range(generator.randrange(5))
        }

    for _ in range(250):
        value = nested(5)
        assert subject.sha256_json(value) == _legacy_sha256_json(value)


def test_streaming_sha256_does_not_build_the_legacy_canonical_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = {
        "symbols": {
            "SH.600000": [["2026-08-25", 10.1, 10.2]],
            "SZ.000001": [["2026-08-25", 11.1, 11.2]],
        }
    }
    expected = _legacy_sha256_json(value)

    def fail_if_materialized(_value: object) -> object:
        raise AssertionError("legacy canonical tree was materialized")

    monkeypatch.setattr(subject, "_canonical_value", fail_if_materialized)

    assert subject.sha256_json(value) == expected


@pytest.mark.parametrize(
    "value, error",
    [
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (Decimal("NaN"), ValueError),
        (datetime(2026, 8, 26, 9, 30), ValueError),
        ({1: "non-string-key"}, TypeError),
        (object(), TypeError),
    ],
)
def test_streaming_sha256_preserves_invalid_value_failures(
    value: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        subject.sha256_json(value)
