"""Fail-closed coordination: every expected case receives a terminal report."""

import time

from redis.exceptions import RedisError

from api_sentinel.models import CaseResult, RunReport, Suite
from api_sentinel.queue import RunQueue


def distributed_run(
    queue: RunQueue,
    suite: Suite,
    contract: dict,
    timeout: float,
    max_deliveries: int = 3,
    base_url: str | None = None,
    executor_options: dict | None = None,
) -> RunReport:
    started = time.monotonic()
    queue.create(
        suite,
        contract,
        timeout=timeout,
        max_deliveries=max_deliveries,
        base_url=base_url,
        executor_options=executor_options,
    )
    results = []
    error = None
    try:
        while True:
            results = queue.results()
            if len(results) == len(suite.cases):
                break
            if time.monotonic() - started >= timeout or not queue.is_open():
                error = "run deadline exceeded; expected results are missing"
                break
            time.sleep(0.05)
    except RedisError:
        error = "Redis unavailable while collecting results"
    finally:
        try:
            queue.close()
        except RedisError:
            error = "Redis unavailable while closing the run"
    complete = {result.case_id for result in results}
    for case in suite.cases:
        if case.id not in complete:
            results.append(
                CaseResult(
                    case_id=case.id,
                    status="error",
                    worker_id="coordinator",
                    message=error or "missing result",
                )
            )
    return RunReport(
        run_id=queue.run_id,
        suite=suite.name,
        mode="distributed",
        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        expected=len(suite.cases),
        results=results,
        infrastructure_error=error,
    )
