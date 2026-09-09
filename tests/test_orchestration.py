import json
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from api_sentinel.cli import main
from api_sentinel.coordinator import distributed_run
from api_sentinel.models import CaseResult, RunReport
from api_sentinel.reporting import write_reports
from api_sentinel.worker import Worker, run_local
from tests.conftest import ROOT


def test_two_workers_execute_contract_suite(queue, suite, document, transport):
    queue.create(suite, document)
    workers = [
        Worker(queue, "http://test", f"w{i}", concurrency=2, transport=transport) for i in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda w: w.run(), workers))
    assert codes == [0, 0]
    assert queue.count() == len(suite.cases)
    assert all(r.status == "passed" for r in queue.results())
    assert {r.worker_id for r in queue.results()} == {"w0", "w1"}


def test_coordinator_waits_and_reports(queue, suite, document, transport):
    worker = Worker(queue, "http://test", "worker", concurrency=2, transport=transport)
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        report = distributed_run(queue, suite, document, timeout=10)
        assert report.exit_code == 0
        assert len(report.results) == len(suite.cases)
    finally:
        worker.stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not queue.is_open()


def test_missing_workers_fail_closed(queue, suite, document):
    report = distributed_run(queue, suite, document, timeout=0.1)
    assert report.exit_code == 2
    assert len(report.results) == len(suite.cases)
    assert all(r.status == "error" for r in report.results)


def test_local_threaded_runner(suite, contract, transport):
    results = run_local(suite, contract, "http://test", "local", 4, transport=transport)
    assert all(r.status == "passed" for r in results)


def test_reports_include_failures_and_escape_xml(tmp_path):
    report = RunReport(
        run_id="r",
        suite="A & <B>",
        mode="local",
        elapsed_ms=500,
        expected=2,
        results=[
            CaseResult(case_id="a", status="failed", worker_id="w", message="<failure>"),
            CaseResult(case_id="b", status="error", worker_id="w", message="bad\x00value"),
        ],
    )
    write_reports(report, tmp_path)
    data = json.loads((tmp_path / "results.json").read_text())
    assert data["summary"]["exit_code"] == 2
    root = ET.parse(tmp_path / "junit.xml").getroot()
    assert root.attrib["failures"] == "1" and root.attrib["errors"] == "1"
    assert root.find("testcase/failure").text == "<failure>"
    assert not list(tmp_path.glob(".*.json.*"))


def test_cli_validation_and_config_failure(tmp_path, capsys):
    assert (
        main(
            [
                "validate",
                "--suite",
                str(ROOT / "examples/suites/smoke.json"),
                "--contract",
                str(ROOT / "examples/contract.json"),
            ]
        )
        == 0
    )
    path = tmp_path / "broken.json"
    path.write_text('{"secret":"do-not-log-this"}')
    assert main(["validate", "--suite", str(path), "--contract", "missing"]) == 2
    assert "do-not-log-this" not in capsys.readouterr().err


def test_exit_codes():
    base = dict(run_id="r", suite="s", mode="local", elapsed_ms=0, expected=1)
    assert RunReport(**base, results=[]).exit_code == 2
    for status, code in [("passed", 0), ("failed", 1), ("error", 2)]:
        report = RunReport(**base, results=[CaseResult(case_id="c", status=status, worker_id="w")])
        assert report.exit_code == code


def test_worker_rejects_mismatched_target(queue, suite, document):
    import pytest

    queue.create(suite, document, base_url="http://expected")
    worker = Worker(queue, "http://different", "w")
    with pytest.raises(ValueError, match="target"):
        worker.run()
    assert queue.count() == 0


def test_coordinator_fails_closed_on_redis_failure(queue, suite, document, monkeypatch):
    from redis.exceptions import ConnectionError

    def fail():
        raise ConnectionError("secret-bearing-url")

    monkeypatch.setattr(queue, "results", fail)
    report = distributed_run(queue, suite, document, timeout=5)
    assert report.exit_code == 2 and len(report.results) == len(suite.cases)
    assert "secret-bearing-url" not in report.model_dump_json()


def test_heartbeat_preserves_a_slow_case(queue, suite, document):
    import time

    import httpx

    suite.cases = suite.cases[:1]
    queue.create(suite, document)

    def handle(_):
        time.sleep(0.35)
        return httpx.Response(200, json={"status": "ok"})

    worker = Worker(
        queue,
        "http://test",
        "w",
        concurrency=1,
        lease_seconds=0.15,
        transport=httpx.MockTransport(handle),
    )
    assert worker.run() == 0
    results = queue.results()
    assert len(results) == 1 and results[0].status == "passed" and results[0].delivery == 1


def test_cli_local_execution(contract, suite, transport, tmp_path, monkeypatch):
    import api_sentinel.cli as module

    def local(suite, contract, base_url, run_id, concurrency, **options):
        return run_local(
            suite, contract, base_url, run_id, concurrency, transport=transport, **options
        )

    monkeypatch.setattr(module, "run_local", local)
    assert (
        main(
            [
                "run",
                "--suite",
                str(ROOT / "examples/suites/smoke.json"),
                "--contract",
                str(ROOT / "examples/contract.json"),
                "--base-url",
                "http://test",
                "--report-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert json.loads((tmp_path / "results.json").read_text())["summary"]["exit_code"] == 0


def test_cli_distributed_transmits_target_and_timeouts(queue, tmp_path, monkeypatch):
    import api_sentinel.cli as module

    captured = {}

    def distributed(queue, suite, contract, timeout, max_deliveries, **options):
        captured.update(options)
        return RunReport(
            run_id=queue.run_id,
            suite=suite.name,
            mode="distributed",
            elapsed_ms=1,
            expected=len(suite.cases),
            results=[
                CaseResult(case_id=case.id, status="passed", worker_id="w") for case in suite.cases
            ],
        )

    monkeypatch.setattr(module, "connect", lambda _: queue.client)
    monkeypatch.setattr(module, "distributed_run", distributed)
    assert (
        main(
            [
                "run",
                "--distributed",
                "--suite",
                str(ROOT / "examples/suites/smoke.json"),
                "--contract",
                str(ROOT / "examples/contract.json"),
                "--base-url",
                "http://target",
                "--request-timeout",
                "2",
                "--case-timeout",
                "15",
                "--report-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert captured == {
        "base_url": "http://target",
        "executor_options": {"timeout": 2, "case_timeout": 15},
    }
