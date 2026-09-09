"""Explicitly scoped OpenAPI 3.1 validator: JSON bodies and scalar parameters.

Contract files are trusted configuration. Remote references are deliberately disabled.
"""

import re
from typing import Any
from urllib.parse import quote

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from api_sentinel.models import Step, Suite

ROOT = "urn:api-sentinel:contract"


class ContractError(ValueError):
    pass


def pointer(document: Any, path: str) -> Any:
    if path == "":
        return document
    if not path.startswith("/"):
        raise ContractError("JSON pointer must start with /")
    current = document
    for token in path[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ContractError("invalid array index in JSON pointer")
            current = current[int(token)]
        else:
            current = current[token]
    return current


class Contract:
    def __init__(self, document: dict):
        if not str(document.get("openapi", "")).startswith("3.1."):
            raise ContractError("only OpenAPI 3.1 is supported; convert older contracts explicitly")
        self.document = document
        self._inspect(document)
        self.registry = Registry().with_resource(
            ROOT, Resource.from_contents(document, default_specification=DRAFT202012)
        )
        self.operations: dict[str, tuple[str, str, dict, list]] = {}
        for path, raw_item in document.get("paths", {}).items():
            item = self.deref(raw_item)
            if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
                raise ContractError("contract paths must be absolute paths without a host or query")
            for method, raw_operation in item.items():
                if method not in {"get", "put", "post", "patch", "delete", "head", "options"}:
                    continue
                operation = self.deref(raw_operation)
                op_id = operation.get("operationId")
                if not op_id or op_id in self.operations:
                    raise ContractError("each operation needs a unique operationId")
                params = {}
                for raw in item.get("parameters", []) + operation.get("parameters", []):
                    param = self.deref(raw)
                    if param["in"] not in {"path", "query", "header"}:
                        raise ContractError("only path, query, and header parameters are supported")
                    schema = self.deref(param.get("schema", {}))
                    if schema.get("type") not in {"string", "integer", "number", "boolean"}:
                        raise ContractError("parameters must declare a scalar type")
                    if "content" in param or "style" in param or "explode" in param:
                        raise ContractError("custom parameter serialization is unsupported")
                    params[(param["in"], param["name"])] = param
                self.operations[op_id] = (method.upper(), path, operation, list(params.values()))
        if not self.operations:
            raise ContractError("contract has no operations")

    def _inspect(self, value: Any):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$id", "$dynamicRef", "$dynamicAnchor"}:
                    raise ContractError(f"{key} is outside the supported schema subset")
                if key == "$ref":
                    if not isinstance(child, str) or not child.startswith("#/"):
                        raise ContractError(
                            "only document-local JSON pointer references are allowed"
                        )
                    try:
                        pointer(self.document, child[1:])
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        raise ContractError("unresolvable contract reference") from exc
                if key == "schema":
                    Draft202012Validator.check_schema(child)
                self._inspect(child)
        elif isinstance(value, list):
            for item in value:
                self._inspect(item)

    def deref(self, value: dict) -> dict:
        visited = set()
        while "$ref" in value:
            ref = value["$ref"]
            if ref in visited:
                raise ContractError("cyclic reference outside a JSON schema")
            visited.add(ref)
            value = pointer(self.document, ref[1:])
        return value

    def errors(self, schema: dict, value: Any, label: str) -> list[str]:
        # Anchor a wrapper at the contract URI so local references resolve there.
        wrapped = {
            "$id": ROOT,
            "components": self.document.get("components", {}),
            "paths": self.document.get("paths", {}),
            "allOf": [schema],
        }
        validator = Draft202012Validator(
            wrapped, registry=self.registry, format_checker=FormatChecker()
        )
        # Never include instance values, schema values, request URLs, or payloads in reports.
        return [
            f"{label}: schema violation ({error.validator})"
            for error in list(validator.iter_errors(value))[:10]
        ]

    def validate_suite(self, suite: Suite):
        for case in suite.cases:
            for step in case.setup + case.steps + case.teardown:
                if step.operation not in self.operations:
                    raise ContractError(f"unknown operation: {step.operation}")
                _, path, op, _ = self.operations[step.operation]
                if set(re.findall(r"\{([^}]+)\}", path)) != set(step.path_params):
                    raise ContractError(f"path parameters do not match: {step.operation}")
                self.response_definition(op, step.expect.status)
                for assertion in step.expect.json_equals:
                    if assertion and not assertion.startswith("/"):
                        raise ContractError("assertion keys must be JSON pointers")

    def request(self, step: Step) -> tuple[str, str, list[str]]:
        method, path, operation, params = self.operations[step.operation]
        errors = []
        for param in params:
            values = {
                "path": step.path_params,
                "query": step.query,
                "header": {k.lower(): v for k, v in step.headers.items()},
            }[param["in"]]
            key = param["name"].lower() if param["in"] == "header" else param["name"]
            if key not in values:
                if param.get("required"):
                    errors.append(f"request: missing required {param['in']} parameter")
            else:
                schema = param["schema"]
                value = values[key]
                # Header values are strings on the wire. Coerce declared primitive types.
                if param["in"] == "header":
                    value = self.coerce(value, self.deref(schema).get("type"))
                errors.extend(self.errors(schema, value, f"request {param['in']} parameter"))
        body = self.deref(operation.get("requestBody", {}))
        if step.json_body is not None:
            schema = body.get("content", {}).get("application/json", {}).get("schema")
            if schema is None:
                errors.append("request: JSON body is not declared")
            else:
                errors.extend(self.errors(schema, step.json_body, "request body"))
        elif body.get("required"):
            errors.append("request: required JSON body is missing")
        for key, value in step.path_params.items():
            # Dot segments must not be normalized into another endpoint.
            encoded = quote(str(value), safe="")
            if encoded in {".", ".."}:
                raise ContractError("dot segments are invalid path parameter values")
            path = path.replace("{" + key + "}", encoded)
        return method, path, [] if step.allow_invalid_request else errors

    @staticmethod
    def coerce(value: str, kind: str | None) -> Any:
        try:
            if kind == "integer":
                return int(value)
            if kind == "number":
                return float(value)
            if kind == "boolean" and value.lower() in {"true", "false"}:
                return value.lower() == "true"
        except ValueError:
            pass
        return value

    def response_definition(self, operation: dict, status: int) -> dict:
        responses = operation.get("responses", {})
        definition = responses.get(
            str(status), responses.get(f"{status // 100}XX", responses.get("default"))
        )
        if definition is None:
            raise ContractError(f"response status {status} is not declared in the contract")
        return self.deref(definition)

    def response_errors(
        self, step: Step, status: int, headers: dict, body: Any, has_body: bool, is_json: bool
    ) -> list[str]:
        _, _, operation, _ = self.operations[step.operation]
        errors = []
        if status != step.expect.status:
            errors.append(f"expected HTTP {step.expect.status}; received {status}")
        try:
            definition = self.response_definition(operation, status)
        except ContractError as exc:
            return errors + [str(exc)]
        content = definition.get("content", {})
        if content:
            if "application/json" not in content:
                return errors + ["response media type is outside supported JSON contracts"]
            if not has_body or not is_json:
                return errors + ["response: expected a JSON body and application/json content type"]
            errors.extend(
                self.errors(content["application/json"].get("schema", {}), body, "response body")
            )
        elif has_body:
            errors.append("response: unexpected body")
        for name, raw in definition.get("headers", {}).items():
            header = self.deref(raw)
            if name.lower() not in headers:
                if header.get("required"):
                    errors.append("response: missing required header")
            elif "schema" in header:
                schema = header["schema"]
                value = self.coerce(headers[name.lower()], self.deref(schema).get("type"))
                errors.extend(self.errors(schema, value, "response header"))
        for path, expected in step.expect.json_equals.items():
            try:
                actual = pointer(body, path)
                if type(actual) is not type(expected) or actual != expected:
                    errors.append("JSON assertion: value mismatch")
            except (KeyError, IndexError, TypeError, ValueError):
                errors.append("JSON assertion: pointer not found")
        return errors
