"""Small JSON CLI for the one-assignment adapter."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from .engine import run_job
from .policy import PolicyError, inspect_policy


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object member")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-adapter")
    parser.add_argument("command", choices=["run", "capabilities"])
    parser.add_argument("--request", required=True, help="JSON request file; '-' reads stdin")
    parser.add_argument("--executable", help="Explicit native Codex executable, without additional arguments")
    args = parser.parse_args(argv)
    try:
        if args.request == "-":
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        else:
            with Path(args.request).open("rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Request exceeds 1 MiB transport limit")
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        print(json.dumps({"contract_version": "adapter/1", "adapter_status": "rejected_preflight",
                          "error": {"code": "request_input", "message": str(exc)}}))
        return 2
    if args.command == "capabilities":
        from .contract import RequestError, parse_request
        try:
            request = parse_request(data)
            report = inspect_policy(request, executable=args.executable)
            report = {key: value for key, value in report.items() if key != "environment"}
            print(json.dumps({"contract_version": "adapter/1", "report": report}))
            return 0
        except (RequestError, PolicyError) as exc:
            print(json.dumps({"contract_version": "adapter/1", "error": {"code": exc.code,
                                                                         "message": str(exc)}}))
            return 2
        except (OSError, ValueError) as exc:
            print(json.dumps({"contract_version": "adapter/1", "error": {"code": "capability_io",
                                                                         "message": type(exc).__name__}}))
            return 2
    cancelled = threading.Event()
    signals = {signal.SIGINT, signal.SIGTERM}
    if hasattr(signal, "SIGBREAK"):
        signals.add(signal.SIGBREAK)
    previous = {number: signal.signal(number, lambda *_: cancelled.set()) for number in signals}
    try:
        result = run_job(data, executable=args.executable, cancel_event=cancelled)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    print(json.dumps(result))
    return 0 if result["adapter_status"] == "completed" else 2
