"""Task5: decide_push 指纹变化判定(纯逻辑)。"""
from cl_app.services.sse_refresh import decide_push


def test_decide_push_first_time():
    push, sig = decide_push(None, {"t": [1]})
    assert push is True
    assert isinstance(sig, str)


def test_decide_push_unchanged():
    cd = {"t": [1, 2], "bis": []}
    _, s1 = decide_push(None, cd)
    push, s2 = decide_push(s1, cd)
    assert push is False
    assert s2 == s1


def test_decide_push_changed():
    _, s1 = decide_push(None, {"t": [1]})
    push, s2 = decide_push(s1, {"t": [1, 2]})
    assert push is True
    assert s2 != s1
