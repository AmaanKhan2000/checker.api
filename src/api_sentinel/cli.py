"""Stable CLI: 0 passed, 1 assertion/contract failure, 2 configuration/infrastructure failure."""

import argparse
import json
import os
import signal
import socket
import sys
import time
import uuid

from api_sentinel.config import load_document, load_suite, validate_base_url
from api_sentinel.contracts import Contract
from api_sentinel.coordinator import distributed_run
from api_sentinel.logging import event
from api_sentinel.models import RunReport
from api_sentinel.queue import RunQueue, connect
from api_sentinel.reporting import write_reports
from api_sentinel.worker import Worker, run_local


def positive_float(value: str) -> float:
    number = float(value)
    if not 0 < number <= 86400:
        raise argparse.ArgumentTypeError("must be greater than zero and at most 86400")
    return number


def concurrency_value(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 128:
        raise argparse.ArgumentTypeError("must be between 1 and 128")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Distributed, contract-driven API test automation")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("validate", "run"):
        child = commands.add_parser(command)
        child.add_argument("--suite", required=True)
        child.add_argument("--contract", required=True)
        if command == "run":
            child.add_argument("--distributed", action="store_true")
            child.add_argument("--run-id", default=None)
            child.add_argument("--report-dir", default="reports/latest")
            child.add_argument("--run-timeout", type=positive_float, default=300)
            child.add_argument("--max-deliveries", type=concurrency_value, default=3)
    worker = commands.add_parser("worker")
    worker.add_argument("--run-id", required=True)
    worker.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    worker.add_argument("--lease-seconds", type=positive_float, default=30)
    worker.add_argument("--wait-seconds", type=positive_float, default=60)
    for child in (commands.choices["run"], worker):
        child.add_argument(
            "--base-url", default=os.getenv("SENTINEL_BASE_URL", "http://127.0.0.1:8000")
        )
        child.add_argument(
            "--redis-url", default=os.getenv("SENTINEL_REDIS_URL", "redis://localhost:6379/0")
        )
        child.add_argument("--concurrency", type=concurrency_value, default=4)
        child.add_argument("--request-timeout", type=positive_float, default=5)
        child.add_argument("--case-timeout", type=positive_float, default=60)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"validate", "run"}:
            suite = load_suite(args.suite)
            document = load_document(args.contract)
            contract = Contract(document)
            contract.validate_suite(suite)
            if args.command == "validate":
                print(json.dumps({"valid": True, "suite": suite.name, "cases": len(suite.cases)}))
                return 0
        base_url = validate_base_url(args.base_url)
        options = {"timeout": args.request_timeout, "case_timeout": args.case_timeout}
        if args.command == "worker":
            client = connect(args.redis_url)
            worker = Worker(
                RunQueue(client, args.run_id),
                base_url,
                args.worker_id,
                concurrency=args.concurrency,
                lease_seconds=args.lease_seconds,
                wait_seconds=args.wait_seconds,
                **options,
            )
            previous = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: worker.stop.set())
            try:
                return worker.run()
            finally:
                client.close()
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        run_id = args.run_id or uuid.uuid4().hex
        if args.distributed:
            client = connect(args.redis_url)
            try:
                report = distributed_run(
                    RunQueue(client, run_id),
                    suite,
                    document,
                    args.run_timeout,
                    args.max_deliveries,
                    base_url=base_url,
                    executor_options=options,
                )
            finally:
                client.close()
        else:
            started = time.monotonic()
            results = run_local(suite, contract, base_url, run_id, args.concurrency, **options)
            report = RunReport(
                run_id=run_id,
                suite=suite.name,
                mode="local",
                expected=len(suite.cases),
                elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                results=results,
            )
        write_reports(report, args.report_dir)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "cases": len(report.results),
                    "elapsed_ms": report.elapsed_ms,
                    "exit_code": report.exit_code,
                }
            )
        )
        return report.exit_code
    except KeyboardInterrupt:
        event("interrupted")
        return 2
    except Exception as exc:
        event("fatal", error_type=type(exc).__name__)
        # Do not print validation exceptions: they may include secret-bearing inputs.
        print(
            "Configuration or infrastructure error. Check your suite, contract, and services.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
