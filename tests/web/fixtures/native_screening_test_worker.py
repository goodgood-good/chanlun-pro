"""Deterministic child used by native screening process-boundary tests."""

from __future__ import annotations

import argparse
from multiprocessing.connection import Client
import os
import time


SCHEMA = "chanlun-trading-screening-native-ipc"
AUTHKEY_ENV = "CHANLUN_SCREENING_WORKER_AUTHKEY"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    connection = Client(
        (args.host, args.port),
        authkey=bytes.fromhex(os.environ.pop(AUTHKEY_ENV)),
    )
    connection.send(
        {
            "schema": SCHEMA,
            "type": "ready",
            "pid": os.getpid(),
            "real_account_access": False,
            "real_order_transport": False,
        }
    )
    while True:
        request = connection.recv()
        identity = request["request_id"]
        if request["type"] == "shutdown":
            return 0
        method = request["method"]
        if method == "crash":
            os._exit(91)
        if method == "hang":
            time.sleep(30)
            continue
        if method == "remote_error":
            connection.send(
                {
                    "schema": SCHEMA,
                    "type": "error",
                    "request_id": identity,
                    "error_type": "ValueError",
                    "message": "deterministic remote failure",
                }
            )
            continue
        connection.send(
            {
                "schema": SCHEMA,
                "type": "progress",
                "request_id": identity,
            }
        )
        connection.send(
            {
                "schema": SCHEMA,
                "type": "result",
                "request_id": identity,
                "value": request["kwargs"],
            }
        )


if __name__ == "__main__":
    raise SystemExit(main())
