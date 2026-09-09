import copy

import pytest
from pydantic import ValidationError

from api_sentinel.config import load_document, render, validate_base_url
from api_sentinel.contracts import Contract, ContractError, pointer
from api_sentinel.models import Case, Step, Suite


def product_step():
    return Step(
        name="read",
        operation="getProduct",
        path_params={"namespace": "n", "product_id": "p"},
        expect={"status": 200},
    )


def test_contract_preflight(contract, suite):
    contract.validate_suite(suite)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(openapi="3.0.3"),
        lambda d: d["components"]["schemas"].update(
            Remote={"$ref": "https://example.com/schema.json"}
        ),
        lambda d: d["components"]["schemas"].update(Missing={"$ref": "#/missing"}),
        lambda d: d["components"]["schemas"].update(Id={"$id": "urn:other"}),
        lambda d: d["paths"]["/health"]["get"].update(operationId="getProduct"),
        lambda d: d["paths"].update({"//evil.example/path": d["paths"]["/health"]}),
        lambda d: d.update(paths={}),
    ],
)
def test_reject_unsupported_contracts(document, mutation):
    mutation(document)
    with pytest.raises(ContractError):
        Contract(document)


def test_resolves_component_schema_and_enforces_types(contract):
    headers = {"content-type": "application/json"}
    valid = {"id": "p", "name": "Phone", "price_cents": 100}
    assert not contract.response_errors(product_step(), 200, headers, valid, True, True)
    valid["price_cents"] = "secret-value"
    errors = contract.response_errors(product_step(), 200, headers, valid, True, True)
    assert errors and "secret-value" not in str(errors)


def test_formats_are_asserted(contract):
    assert contract.errors({"type": "string", "format": "email"}, "bad", "body")
    assert not contract.errors({"type": "string", "format": "email"}, "a@example.com", "body")


def test_required_response_header(contract):
    op = contract.document["paths"]["/health"]["get"]
    op["responses"]["200"]["headers"] = {
        "X-Count": {"required": True, "schema": {"type": "integer"}}
    }
    step = Step(name="health", operation="health", expect={"status": 200})
    assert contract.response_errors(step, 200, {}, {"status": "ok"}, True, True)
    assert not contract.response_errors(step, 200, {"x-count": "3"}, {"status": "ok"}, True, True)


@pytest.mark.parametrize(
    "status,has_body,is_json", [(200, False, False), (200, True, False), (503, True, True)]
)
def test_invalid_responses(contract, status, has_body, is_json):
    assert contract.response_errors(product_step(), status, {}, {}, has_body, is_json)


def test_negative_request_opt_in(contract):
    step = Step(
        name="negative",
        operation="createOrder",
        path_params={"namespace": "n"},
        json_body={"id": "o", "product_id": "p", "quantity": 0},
        expect={"status": 422},
    )
    assert contract.request(step)[2]
    step.allow_invalid_request = True
    assert not contract.request(step)[2]


def test_missing_request_fields_and_header(contract):
    step = Step(
        name="seed",
        operation="seedNamespace",
        path_params={"namespace": "n"},
        expect={"status": 200},
    )
    assert len(contract.request(step)[2]) == 2


def test_dot_segment_rejected(contract):
    step = product_step()
    step.path_params["product_id"] = ".."
    with pytest.raises(ContractError):
        contract.request(step)


def test_path_encoding(contract):
    step = product_step()
    step.path_params["product_id"] = "a/b?c"
    assert contract.request(step)[1].endswith("a%2Fb%3Fc")


def test_json_pointers():
    assert pointer({"a/b": [{"~key": 3}]}, "/a~1b/0/~0key") == 3
    assert pointer([1], "") == [1]
    with pytest.raises(ContractError):
        pointer({}, "invalid")


def test_typed_render_and_missing_env(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    assert render({"seed": "${seed}", "header": "Bearer ${env:TEST_TOKEN}"}, {"seed": 42}) == {
        "seed": 42,
        "header": "Bearer secret",
    }
    with pytest.raises(ValueError):
        render("${unknown}", {})
    monkeypatch.delenv("NOT_CONFIGURED", raising=False)
    with pytest.raises(ValueError):
        render("${env:NOT_CONFIGURED}", {})


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://user:pass@host", "http://host?q=1", "http://host#x"]
)
def test_invalid_base_url(url):
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_config_and_duplicates(tmp_path, suite):
    source = tmp_path / "suite.yaml"
    source.write_text("name: example\nversion: 1\n")
    assert load_document(source)["version"] == 1
    source.write_text("- not-an-object\n")
    with pytest.raises(ValueError):
        load_document(source)
    data = suite.model_dump()
    data["cases"].append(copy.deepcopy(data["cases"][0]))
    with pytest.raises(ValidationError):
        Suite.model_validate(data)
    with pytest.raises(ValidationError):
        Case(id="bad name", steps=[])


def test_preflight_unknown_operation_and_path(contract, suite):
    suite.cases[0].steps[0].operation = "unknown"
    with pytest.raises(ContractError):
        contract.validate_suite(suite)
    suite.cases[0].steps[0].operation = "getProduct"
    with pytest.raises(ContractError):
        contract.validate_suite(suite)


@pytest.mark.parametrize("path", ["/-1", "/01", "/-"])
def test_reject_noncanonical_array_indices(path):
    with pytest.raises(ContractError):
        pointer([1, 2], path)
