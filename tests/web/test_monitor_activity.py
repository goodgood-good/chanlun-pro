from cl_app.services.monitor_activity import describe


def test_silence_is_distinct_from_waiting_images_failures_pause_and_disabled_push():
    state = {
        "enabled": True,
        "dingtalk_enabled": True,
        "pool_size": 3,
        "last_check_at": 300,
        "last_event_at": 100,
        "last_check_notifications": 0,
    }
    delivery = {"configured": True, "pending": 0, "last_sent_at": 105}
    quiet = describe(state, delivery, "running")
    assert (
        quiet["state"] == "quiet"
        and quiet["last_event_at"] == 100
        and quiet["new_events"] == 0
    )
    assert (
        describe(state, {**delivery, "chart_generation": {"pending": 1}}, "running")[
            "state"
        ]
        == "rendering"
    )
    assert (
        describe(
            state,
            {**delivery, "pending": 1, "last_error": "等待信号有效性复核"},
            "running",
        )["state"]
        == "pending"
    )
    assert (
        describe(state, {**delivery, "last_error": "图表生成失败"}, "running")["state"]
        == "failed"
    )
    assert describe(state, delivery, "stopped")["state"] == "unhealthy"
    assert (
        describe({**state, "enabled": False}, delivery, "paused")["state"] == "paused"
    )
    assert (
        describe({**state, "dingtalk_enabled": False}, delivery, "running")["state"]
        == "disabled"
    )
    assert describe({**state, "pool_size": 0}, delivery, "running")["state"] == "empty"
    assert (
        describe(state, {**delivery, "awaiting_enqueue": 3}, "running")["state"]
        == "pending"
    )


def test_new_events_recorded_while_disabled_are_not_claimed_as_delivered():
    state = {
        "enabled": True,
        "dingtalk_enabled": True,
        "last_check_at": 300,
        "last_event_at": 300,
        "last_check_notifications": 1,
    }
    delivery = {"configured": True, "last_sent_at": 105}
    assert describe(state, delivery, "running")["state"] == "recorded"
    assert (
        describe(state, {**delivery, "last_sent_at": 310}, "running")["state"] == "recorded"
    )
    assert describe({**state, "receipts": {"available": True, "expected": 1, "sent": 1}},
                    delivery, "running")["state"] == "sent"
