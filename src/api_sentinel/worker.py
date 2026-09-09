"""Threaded workers with a dedicated heartbeat and graceful draining."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from redis.exceptions import RedisError

from api_sentinel.config import validate_base_url
from api_sentinel.contracts import Contract
from api_sentinel.executor import CaseExecutor
from api_sentinel.logging import event
from api_sentinel.models import CaseResult, Suite
from api_sentinel.queue import Job, RunQueue


class Worker:
    def __init__(
        self,
        queue: RunQueue,
        base_url: str,
        worker_id: str,
        concurrency: int = 4,
        lease_seconds: float = 30,
        wait_seconds: float = 60,
        **executor_options,
    ):
        if concurrency < 1 or concurrency > 128 or lease_seconds < 0.1:
            raise ValueError("invalid concurrency or lease duration")
        self.queue = queue
        self.base_url = base_url
        self.worker_id = worker_id
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.wait_seconds = wait_seconds
        self.executor_options = executor_options
        self.stop = threading.Event()
        self.heartbeat_stop = threading.Event()
        self.lock = threading.Lock()
        self.active: dict[str, tuple[Job, threading.Event]] = {}
        self.error: str | None = None

    def heartbeat(self):
        while not self.heartbeat_stop.wait(self.lease_seconds / 3):
            with self.lock:
                active = list(self.active.values())
            for job, lost in active:
                try:
                    if not self.queue.renew(job, self.lease_seconds):
                        lost.set()
                except RedisError:
                    lost.set()
                    self.error = "Redis heartbeat unavailable"
                    self.stop.set()
                    event("heartbeat_error", worker_id=self.worker_id)

    def consume(self, executor: CaseExecutor, total: int):
        while not self.stop.is_set():
            try:
                job = self.queue.claim(self.lease_seconds)
                if job is None:
                    if self.queue.count() >= total or not self.queue.is_open():
                        return
                    self.stop.wait(0.05)
                    continue
                lost = threading.Event()
                with self.lock:
                    self.active[job.token] = (job, lost)
                try:
                    event(
                        "case_started",
                        run_id=self.queue.run_id,
                        case_id=job.case.id,
                        worker_id=self.worker_id,
                        delivery=job.delivery,
                    )
                    result = executor.run(
                        job.case,
                        self.queue.run_id,
                        self.worker_id,
                        job.delivery,
                        lambda lost=lost: not lost.is_set(),
                    )
                    accepted = self.queue.complete(job, result)
                    event(
                        "case_finished",
                        run_id=self.queue.run_id,
                        case_id=job.case.id,
                        worker_id=self.worker_id,
                        status=result.status,
                        accepted=accepted,
                    )
                finally:
                    with self.lock:
                        self.active.pop(job.token, None)
            except RedisError:
                self.error = "Redis unavailable"
                self.stop.set()
                event("queue_error", worker_id=self.worker_id)
                return
            except Exception as exc:
                self.error = f"worker execution error ({type(exc).__name__})"
                self.stop.set()
                event("worker_error", worker_id=self.worker_id, error_type=type(exc).__name__)
                return

    def run(self) -> int:
        deadline = time.monotonic() + self.wait_seconds
        plan = self.queue.plan()
        while plan is None and not self.stop.wait(0.1):
            if time.monotonic() >= deadline:
                raise TimeoutError("run was not submitted before worker wait deadline")
            plan = self.queue.plan()
        if plan is None:
            return 2
        suite = Suite.model_validate(plan["suite"])
        contract = Contract(plan["contract"])
        contract.validate_suite(suite)
        if plan.get("base_url") and validate_base_url(plan["base_url"]) != validate_base_url(
            self.base_url
        ):
            raise ValueError("worker target does not match the coordinator target")
        executor = CaseExecutor(
            contract,
            self.base_url,
            seed=suite.seed,
            concurrency=self.concurrency,
            **{**self.executor_options, **plan.get("execution", {})},
        )
        heartbeat = threading.Thread(target=self.heartbeat, name="lease-heartbeat", daemon=True)
        heartbeat.start()
        try:
            with ThreadPoolExecutor(
                max_workers=self.concurrency, thread_name_prefix="api-test"
            ) as pool:
                futures = [
                    pool.submit(self.consume, executor, len(suite.cases))
                    for _ in range(self.concurrency)
                ]
                for future in futures:
                    future.result()
        finally:
            self.heartbeat_stop.set()
            heartbeat.join(timeout=5)
            executor.close()
        return 2 if self.error else 0


def run_local(
    suite: Suite, contract: Contract, base_url: str, run_id: str, concurrency: int, **options
) -> list[CaseResult]:
    executor = CaseExecutor(contract, base_url, seed=suite.seed, concurrency=concurrency, **options)
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(executor.run, case, run_id, "local") for case in suite.cases]
            return [future.result() for future in futures]
    finally:
        executor.close()
