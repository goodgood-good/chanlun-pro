"""Scoped acknowledgement for an experimental exclusive native download session.

The deadline covers the RPC, callback acknowledgement and progress delivery.
Python cannot interrupt a blocking native RPC (including stop); the owning
preparation process supplies the outer cancellation boundary. An expired call
must never be accepted even if its final acknowledgement eventually arrives.
"""

import threading
import time


def download(
    client, codes, period, start, end, *, encode, decode, progress=None, timeout=30
):
    expected = set(codes)
    seen = set()
    changed = threading.Event()
    guard = threading.Lock()
    deadline = time.monotonic() + timeout
    state = {"active": True, "error": None, "complete": False, "progress": None}

    def callback(data):
        with guard:
            if not state["active"]:
                return True
            try:
                done, total = data.get("finished"), data.get("total")
                if (
                    type(done) is not int
                    or type(total) is not int
                    or total != len(codes)
                    or not 0 <= done <= total
                ):
                    raise ValueError("QMT batch acknowledgement count mismatch")
                code = data.get("stockcode") or data.get("message")
                if code in expected:
                    seen.add(code)
                elif code:
                    raise ValueError("QMT batch acknowledgement symbol mismatch")
                if data.get("result") is not None and done == total:
                    records = decode(data["result"])
                    if not isinstance(records, dict) or set(records) != expected:
                        raise ValueError("QMT batch result scope mismatch")
                    seen.update(records)
                previous = state["progress"]
                if previous is None or done > previous[0]:
                    state["progress"] = (done, total)
                    changed.set()
                if done == total:
                    if seen != expected:
                        raise ValueError(
                            "QMT batch completion omitted requested symbols"
                        )
                    state["complete"] = True
                    changed.set()
            except Exception as exc:
                state["error"] = exc
                changed.set()
            return state["complete"] or state["error"] is not None

    try:
        client.supply_history_data2(
            codes, period, start, end, encode({"incrementally": False}), callback
        )
        delivered = None
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("QMT completed batch acknowledgement deadline")
            with guard:
                error, complete, latest = (
                    state["error"], state["complete"], state["progress"]
                )
                changed.clear()
            if error is not None:
                raise error
            # Pipe.send and other caller code must never run on the SDK's
            # callback thread or while holding the callback state lock.
            if progress is not None and latest is not None and latest != delivered:
                progress(*latest)
                delivered = latest
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QMT completed batch acknowledgement deadline")
            if complete:
                return True
            changed.wait(remaining)
    except BaseException:
        with guard:
            state["active"] = False
        try:
            client.stop_supply_history_data2()
        except Exception:
            pass
        raise
    finally:
        with guard:
            state["active"] = False
