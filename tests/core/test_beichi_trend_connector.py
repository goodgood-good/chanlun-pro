"""趋势背驰必须比较真实的两中枢连接段与末端离开段。

``ZsCalculator`` 扫描相邻中枢时允许跳过不能成枢的线段，因此后一中枢的
``start`` 并不总与前一中枢的 ``end`` 是同一对象。旧背驰实现假定二者恒等，
会在上涨趋势中拿到一根向下段并在力度比较前直接拒绝合法候选。

连接两个中枢的同向段在当前模型中的稳定语义是前一中枢的完成离开段
``prev_zs.end``；旧快照缺少它时才兼容回退到 ``last_zs.start``。
"""

from __future__ import annotations

from chanlun.core import beichi_calculator as bc
from chanlun.core.types.config import Config


class _Line:
    def __init__(self, direction: str, low: float, high: float) -> None:
        self.type = direction
        self.low = low
        self.high = high
        self.start = object()
        self.end = object()
        self.zs_low = low
        self.zs_high = high


class _Center:
    def __init__(
        self,
        *,
        low: float,
        high: float,
        start: _Line | None,
        end: _Line | None,
    ) -> None:
        self.lines = (
            _Line("up", low, high),
            _Line("down", low, high),
            _Line("up", low, high),
        )
        self.zg = high
        self.zd = low
        self.gg = high
        self.dd = low
        self.start = start
        self.end = end


def test_trend_divergence_prefers_previous_center_exit(monkeypatch) -> None:
    connector = _Line("up", 9, 21)
    wrong_later_start = _Line("down", 20, 25)
    now = _Line("up", 24, 36)
    previous = _Center(low=10, high=20, start=None, end=connector)
    latest = _Center(low=25, high=35, start=wrong_later_start, end=None)
    compared: list[_Line] = []

    def fake_is_beichi(first, second, _provider, _frequency):
        compared.append(first)
        assert second is now
        return True

    monkeypatch.setattr(bc, "is_beichi", fake_is_beichi)
    result = bc.beichi_qs(
        [],
        [previous, latest],
        now,
        lambda _start, _end: {},
        Config.ZS_WZGX_GD.value,
        "30m",
        use_core_envelope=True,
    )

    assert result == (True, [connector])
    assert compared == [connector]


def test_trend_divergence_falls_back_for_legacy_center_without_exit(
    monkeypatch,
) -> None:
    legacy_connector = _Line("up", 9, 21)
    now = _Line("up", 24, 36)
    previous = _Center(low=10, high=20, start=None, end=None)
    latest = _Center(low=25, high=35, start=legacy_connector, end=None)

    monkeypatch.setattr(bc, "is_beichi", lambda first, *_args: first is legacy_connector)
    is_divergent, compared = bc.beichi_qs(
        [],
        [previous, latest],
        now,
        lambda _start, _end: {},
        Config.ZS_WZGX_GD.value,
        "30m",
        use_core_envelope=True,
    )

    assert is_divergent is True
    assert compared == [legacy_connector]


def test_trend_divergence_rejects_when_no_same_direction_connector() -> None:
    down = _Line("down", 9, 21)
    now = _Line("up", 24, 36)
    previous = _Center(low=10, high=20, start=None, end=down)
    latest = _Center(low=25, high=35, start=down, end=None)

    assert bc.beichi_qs(
        [],
        [previous, latest],
        now,
        lambda _start, _end: {},
        Config.ZS_WZGX_GD.value,
        "30m",
        use_core_envelope=True,
    ) == (False, [])
