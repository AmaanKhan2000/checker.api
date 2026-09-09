# Suite reference

The suite root contains `version: 1`, a `name`, an integer `seed` (default 42), and a nonempty `cases` list. Unknown fields are rejected. Case IDs must be unique, 1–100 characters, and contain letters, digits, underscores, dots, or hyphens, beginning with a letter or digit.

Each case has `setup`, `steps`, and `teardown` lists. Only `steps` is required and must be nonempty. Distributed cases must set `replay_safe: true`. Do not use dependencies between cases; express dependent operations as steps within one case.

| Step field | Meaning |
| --- | --- |
| `name` | Human-readable label used in reports. Avoid secrets or personal data. |
| `operation` | Contract `operationId`. Method and path are derived from the contract. |
| `path_params` | Values for every placeholder in the operation path. |
| `query` | Typed scalar query parameters. Integer declarations require integer input. |
| `headers` | String-valued headers; environment references can supply credentials. |
| `json_body` | JSON request body; omitted/`null` means no JSON body. |
| `allow_invalid_request` | Skip request validation for deliberate negative-input tests. Response validation still runs. |
| `expect.status` | Exact required HTTP response status. |
| `expect.json_equals` | Map of RFC 6901 JSON Pointers to required values. Types are compared strictly. |
| `expect.max_latency_ms` | Optional wall-time budget for the step, including connection retries. |

Variables are resolved recursively in values, not in dictionary keys. A whole-string placeholder preserves the variable's type: `"${seed}"` becomes an integer. Interpolated strings such as `"Bearer ${env:API_TOKEN}"` remain strings.

| Variable | Value |
| --- | --- |
| `${seed}` | Suite seed. |
| `${namespace}` | 24-character SHA-256 prefix from run ID, case ID, and delivery attempt. |
| `${run_id}` | Current run identifier. |
| `${case_id}` | Current case identifier. |
| `${env:NAME}` | Environment variable read on the worker. Missing variables cause an error. |

No Python expressions, shell substitutions, or `eval` are supported. Schema/HTTP exception diagnostics exclude instance values and request URLs. Names and IDs remain visible in logs, so keep them non-sensitive.

Preflight validates the suite structure, operation IDs, exact path placeholders, and declared expected statuses. Request data containing variables is validated at execution time after rendering. A `validate` success therefore does not prove credentials are configured or that the remote API will pass.

Example negative test:

```yaml
- name: Reject zero quantity
  operation: createOrder
  path_params:
    namespace: "${namespace}"
  json_body:
    id: order-1
    product_id: product-1
    quantity: 0
  allow_invalid_request: true
  expect:
    status: 422
```

Use a separate report directory for concurrent coordinator runs. JSON and XML files are individually atomic; they are not a transactional pair. Each file embeds the run ID so consumers can detect mismatched artifacts after a crash.
