import pickle
import zlib

import pytest

from chanlun.screening import worker_transport as transport


def task(tmp_path):
    return {"ticket": "a" * 32 + "_0", "run_dir": str(tmp_path)}


def test_small_result_uses_bounded_pipe_without_a_temporary_file(tmp_path):
    item = task(tmp_path)
    result = {
        "code": "SH.600000",
        "name": "浦发银行",
        "rows": [{"selected": [], "data_errors": []}],
    }
    message = transport.store_reply(item, result, prefer_inline=True)
    assert len(pickle.dumps(message)) <= transport.MAX_CONTROL_BYTES
    assert "result_inline" in message and not list(tmp_path.iterdir())
    assert transport.read_reply(message, item["ticket"], tmp_path) == result
    assert transport.discard_reply(message, item["ticket"], tmp_path)


def test_large_result_keeps_the_file_protocol(tmp_path):
    item = task(tmp_path)
    result = {"code": "SH.600000", "facts": "x" * (transport.MAX_INLINE_BYTES + 1)}
    message = transport.store_reply(item, result, prefer_inline=True)
    assert "result_file" in message
    assert transport.read_reply(message, item["ticket"], tmp_path) == result
    assert transport.discard_reply(message, item["ticket"], tmp_path)


@pytest.mark.parametrize(
    "damage",
    ["ticket", "checksum", "size", "overflow", "truncated", "trailing", "ambiguous"],
)
def test_bad_inline_responses_cannot_be_accepted(tmp_path, damage):
    item = task(tmp_path)
    message = transport.store_reply(item, {"rows": []}, prefer_inline=True)
    value = message["result_inline"]
    if damage == "ticket":
        message["ticket"] = "b" * 32 + "_0"
    elif damage == "checksum":
        value["sha256"] = "0" * 64
    elif damage == "size":
        value["bytes"] -= 1
    elif damage == "overflow":
        value["data"] = zlib.compress(b"x" * (transport.MAX_INLINE_BYTES + 1))
    elif damage == "truncated":
        value["data"] = value["data"][:-1]
    elif damage == "trailing":
        value["data"] += b"unexpected"
    else:
        message["result_file"] = {}
    with pytest.raises(ValueError):
        transport.read_reply(message, item["ticket"], tmp_path)
