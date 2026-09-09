"""Measure serial vs multi-process execution against the same API and suite.

Includes worker startup, queue coordination, fixtures, validation, and cleanup.
Does not turn a synthetic demo measurement into a production performance claim.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from api_sentinel.config import load_document, load_suite
from api_sentinel.contracts import Contract
from api_sentinel.coordinator import distributed_run
from api_sentinel.models import RunReport
from api_sentinel.queue import RunQueue, connect
from api_sentinel.reporting import atomic_write, write_reports
from api_sentinel.worker import run_local

ROOT = Path(__file__).resolve().parents[1]


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def distributed(suite, document, base_url, redis_url, output, workers=2, threads=4):
    run_id = uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=True)
    client = connect(redis_url)
    queue = RunQueue(client, run_id)
    processes = []
    with ExitStack() as stack:
        try:
            for index in range(workers):
                log = stack.enter_context((output / f"worker-{index}.log").open("w"))
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "api_sentinel.cli",
                            "worker",
                            "--run-id",
                            run_id,
                            "--worker-id",
                            f"process-{index}",
                            "--redis-url",
                            redis_url,
                            "--base-url",
                            base_url,
                            "--concurrency",
                            str(threads),
                        ],
                        stdout=log,
                        stderr=log,
                        cwd=ROOT,
                        env=os.environ.copy(),
                    )
                )
            report = distributed_run(queue, suite, document, timeout=120, base_url=base_url)
            for process in processes:
                if process.wait(timeout=15) != 0:
                    raise RuntimeError("worker process exited with an infrastructure error")
            write_reports(report, output)
            if report.exit_code != 0:
                raise RuntimeError(f"benchmark or acceptance run failed; inspect {output}")
            return report
        finally:
            for process in processes:
                stop_process(process)
            queue.delete()
            client.close()


def benchmark(
    suite_path, contract_path, base_url, redis_url, output, repeats=3, workers=2, threads=4
):
    suite = load_suite(suite_path)
    document = load_document(contract_path)
    contract = Contract(document)
    contract.validate_suite(suite)
    output.mkdir(parents=True, exist_ok=True)
    timings = {"serial": [], "distributed": []}
    for round_index in range(repeats + 1):
        # One discarded warmup, then alternate ordering to reduce systematic bias.
        ordering = ("serial", "distributed") if round_index % 2 == 0 else ("distributed", "serial")
        for mode in ordering:
            destination = output / f"round-{round_index}-{mode}"
            if mode == "serial":
                started = time.monotonic()
                run_id = uuid.uuid4().hex
                results = run_local(suite, contract, base_url, run_id, 1)
                report = RunReport(
                    run_id=run_id,
                    suite=suite.name,
                    mode="local",
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    expected=len(suite.cases),
                    results=results,
                )
                write_reports(report, destination)
                if report.exit_code != 0:
                    raise RuntimeError("serial benchmark failed")
            else:
                report = distributed(
                    suite, document, base_url, redis_url, destination, workers, threads
                )
            if round_index:
                timings[mode].append(round(report.elapsed_ms, 3))
    serial = statistics.median(timings["serial"])
    parallel = statistics.median(timings["distributed"])
    data = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cases": len(suite.cases),
        "workers": workers,
        "threads_per_worker": threads,
        "warmup_rounds_excluded": 1,
        "measured_rounds": repeats,
        "measurements_ms": timings,
        "serial_median_ms": serial,
        "distributed_median_ms": parallel,
        "time_reduction_percent": round((serial - parallel) / serial * 100, 2),
        "speedup": round(serial / parallel, 3),
        "scope": "This environment only; includes process startup and fixture overhead.",
    }
    atomic_write(output / "benchmark.json", json.dumps(data, indent=2) + "\n")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="examples/suites/benchmark.json")
    parser.add_argument("--contract", default="examples/contract.json")
    parser.add_argument(
        "--base-url", default=os.getenv("SENTINEL_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--redis-url", default=os.getenv("SENTINEL_REDIS_URL", "redis://127.0.0.1:6379/0")
    )
    parser.add_argument("--output", type=Path, default=Path("reports/benchmark"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    if not (1 <= args.repeats <= 30 and 1 <= args.workers <= 16 and 1 <= args.threads <= 128):
        parser.error("invalid repeat, worker, or thread count")
    print(
        json.dumps(
            benchmark(
                args.suite,
                args.contract,
                args.base_url,
                args.redis_url,
                args.output,
                args.repeats,
                args.workers,
                args.threads,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
