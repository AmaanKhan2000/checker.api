"""Bounded HTTP execution with isolated fixtures and value-free diagnostics."""

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from api_sentinel.config import render, validate_base_url
from api_sentinel.contracts import Contract, ContractError
from api_sentinel.models import Case, CaseResult, Step, StepResult


class CaseExecutor:
    def __init__(
        self,
        contract: Contract,
        base_url: str,
        seed: int = 42,
        concurrency: int = 4,
        timeout: float = 5,
        case_timeout: float = 60,
        max_response_bytes: int = 2_000_000,
        connect_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ):
        self.contract = contract
        self.base_url = validate_base_url(base_url)
        self.seed = seed
        self.timeout = timeout
        self.case_timeout = case_timeout
        self.max_response_bytes = max_response_bytes
        self.connect_retries = connect_retries
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 3)),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def run(
        self,
        case: Case,
        run_id: str,
        worker_id: str,
        delivery: int = 1,
        lease_valid: Callable[[], bool] = lambda: True,
    ) -> CaseResult:
        started = time.monotonic()
        namespace = hashlib.sha256(f"{run_id}:{case.id}:{delivery}".encode()).hexdigest()[:24]
        variables = {
            "namespace": namespace,
            "seed": self.seed,
            "case_id": case.id,
            "run_id": run_id,
        }
        results = []
        deadline = started + self.case_timeout
        try:
            for phase, steps in (("setup", case.setup), ("test", case.steps)):
                for step in steps:
                    result = self.step(step, phase, variables, deadline, lease_valid)
                    results.append(result)
                    if result.status != "passed":
                        break
                if any(result.status != "passed" for result in results):
                    break
        finally:
            # Cleanup has its own small budget, but must never mutate after a lost lease.
            cleanup_deadline = time.monotonic() + min(self.case_timeout, 15)
            for step in case.teardown:
                results.append(
                    self.step(step, "teardown", variables, cleanup_deadline, lease_valid)
                )
        status = (
            "error"
            if any(r.status == "error" for r in results)
            else ("failed" if any(r.status == "failed" for r in results) else "passed")
        )
        return CaseResult(
            case_id=case.id,
            status=status,
            worker_id=worker_id,
            delivery=delivery,
            elapsed_ms=round((time.monotonic() - started) * 1000, 3),
            steps=results,
        )

    def step(
        self,
        raw_step: Step,
        phase: str,
        variables: dict[str, Any],
        deadline: float,
        lease_valid: Callable[[], bool],
    ) -> StepResult:
        started = time.monotonic()
        attempts = 0
        status_code = None
        try:
            step = Step.model_validate(render(raw_step.model_dump(), variables))
            method, path, failures = self.contract.request(step)
            if failures:
                return StepResult(
                    name=raw_step.name, phase=phase, status="failed", messages=failures
                )
            while True:
                if not lease_valid():
                    raise RuntimeError("lease lost")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("case deadline exceeded")
                attempts += 1
                headers = {
                    **step.headers,
                    "X-Sentinel-Run": str(variables["run_id"]),
                    "X-Sentinel-Namespace": str(variables["namespace"]),
                }
                try:
                    request_kwargs = {
                        "params": step.query,
                        "headers": headers,
                        "timeout": min(remaining, self.timeout),
                    }
                    if step.json_body is not None:
                        request_kwargs["json"] = step.json_body
                    with self.client.stream(method, self.base_url + path, **request_kwargs) as resp:
                        status_code = resp.status_code
                        chunks = []
                        size = 0
                        for chunk in resp.iter_bytes():
                            size += len(chunk)
                            if size > self.max_response_bytes:
                                raise RuntimeError("response size limit exceeded")
                            if time.monotonic() >= deadline:
                                raise TimeoutError("case deadline exceeded")
                            if not lease_valid():
                                raise RuntimeError("lease lost")
                            chunks.append(chunk)
                        content = b"".join(chunks)
                        media_type = resp.headers.get("content-type", "").split(";")[0].lower()
                        response_headers = dict(resp.headers)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    # No assertion retries; never automatically replay writes over HTTP.
                    if method not in {"GET", "HEAD", "OPTIONS"} or attempts > self.connect_retries:
                        raise
                    time.sleep(min(0.05 * attempts, max(0, deadline - time.monotonic())))
            elapsed = (time.monotonic() - started) * 1000
            is_json = media_type == "application/json"
            try:
                body = json.loads(content) if content and is_json else None
            except (ValueError, UnicodeDecodeError):
                body, is_json = None, False
            failures = self.contract.response_errors(
                step, status_code, response_headers, body, bool(content), is_json
            )
            if step.expect.max_latency_ms and elapsed > step.expect.max_latency_ms:
                failures.append("latency budget exceeded")
            return StepResult(
                name=raw_step.name,
                phase=phase,
                status="failed" if failures else "passed",
                elapsed_ms=round(elapsed, 3),
                http_status=status_code,
                attempts=attempts,
                messages=failures,
            )
        except Exception as exc:
            # Exception messages can contain credentials, response values, and target URLs.
            category = (
                "configuration" if isinstance(exc, (ValueError, ContractError)) else "execution"
            )
            return StepResult(
                name=raw_step.name,
                phase=phase,
                status="error",
                elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                http_status=status_code,
                attempts=attempts,
                messages=[f"{category} error ({type(exc).__name__})"],
            )
