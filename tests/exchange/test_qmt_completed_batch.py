import json
from types import SimpleNamespace
import pytest
from cl_app.services.qmt_completed_batch import download


def run(events, *, returned=True):
    calls = []
    stopped = []

    def supply(codes, period, start, end, param, callback):
        calls.append((codes, period, start, end, json.loads(param)))
        for event in events:
            callback(event)
        return returned

    client = SimpleNamespace(
        supply_history_data2=supply,
        stop_supply_history_data2=lambda: stopped.append(True),
    )
    return client, calls, stopped


def test_only_a_complete_scoped_callback_can_acknowledge_the_exact_range():
    client, calls, stopped = run(
        [
            {"finished": 1, "total": 2, "message": "one"},
            {"finished": 2, "total": 2, "message": "two"},
        ],
        returned=False,
    )
    assert download(
        client,
        ["one", "two"],
        "1m",
        "20261028000000",
        "20261028144001",
        encode=json.dumps,
        decode=json.loads,
        timeout=0.01,
    )
    assert calls == [
        (
            ["one", "two"],
            "1m",
            "20261028000000",
            "20261028144001",
            {"incrementally": False},
        )
    ]
    assert not stopped


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"finished": 2, "total": 2, "message": "one"}],
        [{"finished": 2, "total": 2, "message": "wrong"}],
        [{"finished": 3, "total": 3, "message": "one"}],
        [
            {"finished": 1, "total": 2, "message": "one"},
            {"finished": 2, "total": 2, "message": "one"},
        ],
        [{"finished": -1, "total": -1, "message": "failed"}],
    ],
)
def test_return_value_or_partial_foreign_and_duplicate_callbacks_never_prove_completion(
    events,
):
    client, _, stopped = run(events)
    with pytest.raises((TimeoutError, ValueError)):
        download(
            client,
            ["one", "two"],
            "5m",
            "start",
            "end",
            encode=json.dumps,
            decode=json.loads,
            timeout=0.01,
        )
    assert stopped == [True]


def test_scoped_result_map_can_prove_all_members_without_message_labels():
    client, _, _ = run(
        [{"finished": 2, "total": 2, "result": json.dumps({"one": {}, "two": {}})}]
    )
    assert download(
        client,
        ["one", "two"],
        "5m",
        "start",
        "end",
        encode=json.dumps,
        decode=json.loads,
        timeout=0.01,
    )
