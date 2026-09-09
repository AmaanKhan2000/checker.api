import httpx
import pytest

from api_sentinel.executor import CaseExecutor
from api_sentinel.models import Case, Step


def health_case(**expectation):
    return Case(
        id="health",
        steps=[Step(name="health", operation="health", expect={"status": 200, **expectation})],
    )


def execute(contract, handler, case=None, **options):
    runner = CaseExecutor(
        contract, "http://test", transport=httpx.MockTransport(handler), **options
    )
    try:
        return runner.run(case or health_case(), "run", "worker")
    finally:
        runner.close()


def test_full_acceptance_suite(contract, suite, transport):
    executor = CaseExecutor(contract, "http://test", transport=transport)
    try:
        results = [executor.run(case, "test", "worker") for case in suite.cases]
        assert all(r.status == "passed" for r in results), results
    finally:
        executor.close()


def test_contract_regression_fails(contract, suite, transport, monkeypatch):
    monkeypatch.setenv("DEMO_BREAK_CONTRACT", "true")
    executor = CaseExecutor(contract, "http://test", transport=transport)
    try:
        result = executor.run(suite.cases[2], "broken", "worker")
        assert result.status == "failed"
        assert result.steps[-1].phase == "teardown"
        assert result.steps[-1].status == "passed"
    finally:
        executor.close()


def test_secret_values_do_not_leak(contract):
    result = execute(contract, lambda _: httpx.Response(200, json={"status": "sensitive-secret"}))
    assert result.status == "failed"
    assert "sensitive-secret" not in result.model_dump_json()


def test_get_connection_retry(contract):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("credential-bearing-url")
        return httpx.Response(200, json={"status": "ok"})

    result = execute(contract, handler)
    assert result.status == "passed" and result.steps[0].attempts == 2


def test_assertion_failures_not_retried(contract):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={"secret": "never log me"})

    result = execute(contract, handler)
    assert len(calls) == 1 and result.status == "failed"


def test_post_not_retried(contract):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("private-token")

    case = Case(
        id="write",
        steps=[
            Step(
                name="post",
                operation="createOrder",
                path_params={"namespace": "n"},
                json_body={"id": "o", "product_id": "p", "quantity": 1},
                expect={"status": 201},
            )
        ],
    )
    result = execute(contract, handler, case)
    assert len(calls) == 1 and result.status == "error"
    assert "private-token" not in result.model_dump_json()


def test_read_timeout_not_retried(contract):
    def handler(_):
        raise httpx.ReadTimeout("secret")

    result = execute(contract, handler)
    assert result.status == "error" and result.steps[0].attempts == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json", headers={"content-type": "application/json"}),
        httpx.Response(200, content=b"{}", headers={"content-type": "text/plain"}),
        httpx.Response(302, headers={"location": "https://example.com"}),
    ],
)
def test_bad_response_and_redirect(contract, response):
    result = execute(contract, lambda _: response)
    assert result.status == "failed"


def test_response_cap(contract):
    result = execute(
        contract, lambda _: httpx.Response(200, content=b"x" * 100), max_response_bytes=10
    )
    assert result.status == "error"


def test_latency_budget(contract):
    case = health_case(max_latency_ms=0.000001)
    result = execute(contract, lambda _: httpx.Response(200, json={"status": "ok"}), case)
    assert result.status == "failed"


def test_json_value_assertion(contract):
    result = execute(
        contract,
        lambda _: httpx.Response(200, json={"status": "ok"}),
        health_case(json_equals={"/status": "different"}),
    )
    assert result.status == "failed"


def test_no_requests_after_lost_lease(contract):
    calls = []
    runner = CaseExecutor(
        contract, "http://test", transport=httpx.MockTransport(lambda r: calls.append(r))
    )
    try:
        result = runner.run(health_case(), "run", "w", lease_valid=lambda: False)
        assert result.status == "error" and not calls
    finally:
        runner.close()


def test_cleanup_after_setup_failure(contract, suite, transport, monkeypatch):
    monkeypatch.setenv("TEST_SUPPORT_TOKEN", "wrong")
    # Remove the required worker environment variable to force setup configuration failure.
    monkeypatch.delenv("TEST_SUPPORT_TOKEN")
    executor = CaseExecutor(contract, "http://test", transport=transport)
    try:
        result = executor.run(suite.cases[1], "run", "worker")
        assert [r.phase for r in result.steps] == ["setup", "teardown"]
        assert result.status == "error"
    finally:
        executor.close()


def test_delivery_namespaces_are_isolated(contract, suite, transport):
    executor = CaseExecutor(contract, "http://test", transport=transport)
    namespaces = []
    original = executor.step

    def capture(step, phase, variables, deadline, lease_valid):
        namespaces.append(variables["namespace"])
        return original(step, phase, variables, deadline, lease_valid)

    executor.step = capture
    try:
        executor.run(suite.cases[1], "run", "w", delivery=1)
        first = namespaces[0]
        namespaces.clear()
        executor.run(suite.cases[1], "run", "w", delivery=2)
        assert first != namespaces[0]
    finally:
        executor.close()
