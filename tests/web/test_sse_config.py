"""Task1: SSE feature flag 与刷新间隔配置存在且取值合理。"""
from chanlun import config


def test_sse_flags_exist():
    assert isinstance(config.ENABLE_SSE_PUSH, bool)
    assert config.SSE_REFRESH_MS >= 1000
    assert config.SSE_REFRESH_MS_US >= config.SSE_REFRESH_MS
