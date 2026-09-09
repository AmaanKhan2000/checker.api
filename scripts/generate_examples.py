"""Generate the checked-in consumer-owned contract and example suites.

This is intentionally independent of app.openapi(): implementation drift must
not silently rewrite the expected contract during CI.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def schema(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def response(model=None):
    result = {"description": "Expected response"}
    if model is not None:
        result["content"] = {"application/json": {"schema": model}}
    return result


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


string = {"type": "string"}
integer = {"type": "integer"}
namespace = {"in": "path", "name": "namespace", "required": True, "schema": string}
token = {"in": "header", "name": "X-Test-Token", "required": True, "schema": string}
error = response(ref("Error"))
contract = {
    "openapi": "3.1.0",
    "info": {"title": "Inventory Consumer Contract", "version": "1.0.0"},
    "components": {
        "schemas": {
            "Product": schema(
                {"id": string, "name": string, "price_cents": {"type": "integer", "minimum": 0}}
            ),
            "Error": schema({"detail": string}),
            "OrderInput": schema(
                {
                    "id": {"type": "string", "pattern": "^[a-z0-9-]{1,50}$"},
                    "product_id": string,
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 10},
                }
            ),
            "Order": schema(
                {
                    "id": string,
                    "product_id": string,
                    "quantity": integer,
                    "total_cents": integer,
                    "status": {"const": "confirmed"},
                }
            ),
        }
    },
    "paths": {
        "/health": {
            "get": {
                "operationId": "health",
                "responses": {"200": response(schema({"status": {"const": "ok"}}))},
            }
        },
        "/test-support/namespaces/{namespace}": {
            "parameters": [namespace, token],
            "put": {
                "operationId": "seedNamespace",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": schema({"seed": integer})}},
                },
                "responses": {
                    "200": response(
                        schema({"namespace": string, "seed": integer, "products": integer})
                    ),
                    "403": error,
                    "409": error,
                },
            },
            "delete": {
                "operationId": "deleteNamespace",
                "responses": {"204": response(), "403": error},
            },
        },
        "/catalog/{namespace}/products": {
            "parameters": [namespace],
            "get": {
                "operationId": "listProducts",
                "parameters": [
                    {
                        "in": "query",
                        "name": "limit",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                    }
                ],
                "responses": {
                    "200": response(schema({"items": {"type": "array", "items": ref("Product")}})),
                    "422": response({"type": "object", "required": ["detail"]}),
                },
            },
        },
        "/catalog/{namespace}/products/{product_id}": {
            "parameters": [
                namespace,
                {"in": "path", "name": "product_id", "required": True, "schema": string},
            ],
            "get": {
                "operationId": "getProduct",
                "responses": {"200": response(ref("Product")), "404": error},
            },
        },
        "/orders/{namespace}": {
            "parameters": [namespace],
            "post": {
                "operationId": "createOrder",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": ref("OrderInput")}},
                },
                "responses": {
                    "201": response(ref("Order")),
                    "404": error,
                    "409": error,
                    "422": response({"type": "object", "required": ["detail"]}),
                },
            },
        },
    },
}
path = {"namespace": "${namespace}"}
headers = {"X-Test-Token": "${env:TEST_SUPPORT_TOKEN}"}
setup = [
    {
        "name": "seed isolated catalog",
        "operation": "seedNamespace",
        "path_params": path,
        "headers": headers,
        "json_body": {"seed": "${seed}"},
        "expect": {"status": 200, "json_equals": {"/seed": "${seed}", "/products": 3}},
    }
]
teardown = [
    {
        "name": "remove isolated data",
        "operation": "deleteNamespace",
        "path_params": path,
        "headers": headers,
        "expect": {"status": 204},
    }
]


def case(name, steps):
    return {"id": name, "replay_safe": True, "setup": setup, "steps": steps, "teardown": teardown}


def step(name, op, status=200, **kwargs):
    expectation = {"status": status, **kwargs.pop("expect", {})}
    return {"name": name, "operation": op, "path_params": path, "expect": expectation, **kwargs}


order = {"id": "order-1", "product_id": "product-1", "quantity": 2}
cases = [
    {
        "id": "health",
        "replay_safe": True,
        "steps": [{"name": "health is ready", "operation": "health", "expect": {"status": 200}}],
    },
    case(
        "catalog-list",
        [
            step(
                "list deterministic products",
                "listProducts",
                query={"limit": 2},
                expect={"json_equals": {"/items/0/id": "product-1", "/items/1/name": "Tablet"}},
            )
        ],
    ),
    case(
        "product-details",
        [
            step(
                "get a product",
                "getProduct",
                path_params={**path, "product_id": "product-1"},
                expect={"json_equals": {"/name": "Phone"}},
            )
        ],
    ),
    case(
        "product-not-found",
        [
            step(
                "missing product returns 404",
                "getProduct",
                404,
                path_params={**path, "product_id": "missing"},
            )
        ],
    ),
    case(
        "order-idempotency",
        [
            step(
                "create order",
                "createOrder",
                201,
                json_body=order,
                expect={"json_equals": {"/quantity": 2, "/status": "confirmed"}},
            ),
            step("replay identical order", "createOrder", 201, json_body=order),
            step(
                "reject conflicting duplicate",
                "createOrder",
                409,
                json_body={**order, "quantity": 3},
            ),
        ],
    ),
    case(
        "invalid-order",
        [
            step(
                "reject zero quantity",
                "createOrder",
                422,
                json_body={**order, "quantity": 0},
                allow_invalid_request=True,
            )
        ],
    ),
    case(
        "invalid-query",
        [
            step(
                "reject invalid page size",
                "listProducts",
                422,
                query={"limit": 0},
                allow_invalid_request=True,
            )
        ],
    ),
    case(
        "missing-order-product",
        [
            step(
                "reject unknown product",
                "createOrder",
                404,
                json_body={**order, "product_id": "missing"},
            )
        ],
    ),
]
for filename, data in [
    ("contract.json", contract),
    (
        "suites/smoke.json",
        {"version": 1, "name": "Inventory API acceptance", "seed": 42, "cases": cases},
    ),
    (
        "suites/benchmark.json",
        {
            "version": 1,
            "name": "Inventory API benchmark",
            "seed": 42,
            "cases": [
                case(f"catalog-{i:03}", [step("catalog request", "listProducts")])
                for i in range(60)
            ],
        },
    ),
]:
    destination = ROOT / "examples" / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2) + "\n")

# A larger workload makes worker startup overhead visible alongside the small benchmark.
throughput = {
    "version": 1,
    "name": "Inventory API throughput benchmark",
    "seed": 42,
    "cases": [
        case(f"catalog-{i:03}", [step("catalog request", "listProducts")]) for i in range(240)
    ],
}
(ROOT / "examples/suites/throughput.json").write_text(json.dumps(throughput, indent=2) + "\n")
