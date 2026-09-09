"""Real process integration, crash recovery, regression detection, and benchmark.

Requires an isolated Redis instance. Starts/stops its own local demo API only.
All queue keys use random run IDs; never flushes Redis or changes global config.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
from benchmark import ROOT, benchmark, distributed, stop_process

from api_sentinel.config import load_document, load_suite
from api_sentinel.queue import RunQueue, connect
from api_sentinel.reporting import atomic_write


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, httpx.HTTPError):
            pass
        time.sleep(0.02)
    raise TimeoutError("service or run did not become ready")


@contextmanager
def api(output, broken=False):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="sentinel-api-") as directory:
        env = {
            **os.environ,
            "DEMO_ENABLE_TEST_SUPPORT": "true",
            "DEMO_DB_PATH": str(Path(directory) / "demo.sqlite3"),
            "DEMO_DELAY_SECONDS": "0.02",
            "DEMO_BREAK_CONTRACT": str(broken).lower(),
        }
        with output.open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "examples.demo_api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                env=env,
                stdout=log,
                stderr=log,
                cwd=ROOT,
            )
            base_url = f"http://127.0.0.1:{port}"
            try:
                with httpx.Client(trust_env=False) as client:
                    wait_for(lambda: client.get(base_url + "/health", timeout=1).status_code == 200)
                yield base_url
            finally:
                stop_process(process)


def crash_recovery(redis_url, base_url, output):
    suite = load_suite(ROOT / "examples/suites/benchmark.json")
    suite.cases = suite.cases[:1]
    document = load_document(ROOT / "examples/contract.json")
    client = connect(redis_url)
    queue = RunQueue(client, uuid.uuid4().hex)
    queue.create(suite, document, timeout=30, base_url=base_url)
    common = [
        sys.executable,
        "-m",
        "api_sentinel.cli",
        "worker",
        "--run-id",
        queue.run_id,
        "--redis-url",
        redis_url,
        "--base-url",
        base_url,
        "--concurrency",
        "1",
        "--lease-seconds",
        "0.5",
    ]
    with output.open("w") as log:
        crashed = subprocess.Popen(
            [*common, "--worker-id", "crashed-worker"], cwd=ROOT, stdout=log, stderr=log
        )
        survivor = None
        try:
            wait_for(lambda: client.hlen(queue.keys[4]) == 1)
            crashed.send_signal(signal.SIGKILL)
            crashed.wait(timeout=5)
            survivor = subprocess.Popen(
                [*common, "--worker-id", "recovery-worker"], cwd=ROOT, stdout=log, stderr=log
            )
            wait_for(lambda: queue.count() == 1)
            result = queue.results()[0]
            assert result.status == "passed" and result.delivery == 2
            assert result.worker_id == "recovery-worker"
            assert survivor.wait(timeout=10) == 0
            return result.model_dump()
        finally:
            stop_process(crashed)
            if survivor:
                stop_process(survivor)
            queue.close()
            queue.delete()
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--output", type=Path, default=Path("reports/verification"))
    parser.add_argument("--skip-benchmark", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["TEST_SUPPORT_TOKEN"] = uuid.uuid4().hex
    client = connect(args.redis_url)
    redis_version = client.info("server")["redis_version"]
    client.close()
    suite = load_suite(ROOT / "examples/suites/smoke.json")
    document = load_document(ROOT / "examples/contract.json")
    evidence = {"redis_version": redis_version, "docker_executed": False}
    with api(args.output / "api.log") as base_url:
        report = distributed(suite, document, base_url, args.redis_url, args.output / "distributed")
        evidence["distributed"] = {
            "cases_passed": len(report.results),
            "exit_code": report.exit_code,
            "workers": sorted({r.worker_id for r in report.results}),
        }
        evidence["crash_recovery"] = crash_recovery(
            args.redis_url, base_url, args.output / "crash-recovery.log"
        )
        if not args.skip_benchmark:
            evidence["benchmark"] = benchmark(
                ROOT / "examples/suites/benchmark.json",
                ROOT / "examples/contract.json",
                base_url,
                args.redis_url,
                args.output / "benchmark",
            )
    with api(args.output / "broken-api.log", broken=True) as base_url:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "api_sentinel.cli",
                "run",
                "--suite",
                "examples/suites/smoke.json",
                "--contract",
                "examples/contract.json",
                "--base-url",
                base_url,
                "--report-dir",
                str(args.output / "broken-contract"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 1, completed.stdout + completed.stderr
        evidence["intentional_regression_exit_code"] = completed.returncode
    atomic_write(args.output / "verification.json", json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
