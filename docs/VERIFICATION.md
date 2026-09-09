# Verification evidence

Verified on September 9, 2026 in Linux x86-64, Python 3.12.14, using an actual Redis 6.2.14 server and separate Python worker processes.

| Check | Observed outcome |
| --- | --- |
| Unit and integration tests | **86 passed**, 0 failed, 0 skipped. Includes real Redis and Lua-backed emulated Redis. |
| Combined statement/branch coverage | **89.45%**; CI minimum is 80%. |
| Statement coverage | 90.90%. |
| Branch coverage | 85.12%. |
| Distributed acceptance | 8/8 cases passed; both independent workers returned accepted results. |
| Crash recovery | A worker received SIGKILL after claiming a case; another worker completed delivery 2 successfully. |
| Deliberate response-schema regression | CLI returned **1**, and generated failing JSON/JUnit artifacts. |
| Missing workers | Coordinator returned infrastructure failure and emitted an error for every missing case. |
| Stale result fencing | Expired and superseded tokens were rejected in real Redis tests. |
| Heartbeats | A case longer than its initial lease completed on delivery 1. |
| Target mismatch | Worker rejected a target differing from the submitted plan. |
| Code quality | Ruff lint and formatting checks passed. |
| Packaging | Wheel and source archive built. Wheel installed in a fresh environment and its CLI validated the sample suite. |
| Configuration | Compose/CI YAML parsed; Bash entry script passed syntax checking. |

The local test environment lacked Docker, so Docker image builds, container startup, and the GitHub Actions workflow were **not executed here**. The checked-in CI is designed to perform those checks once pushed to a repository. No claim is made that the project was deployed to a production environment. Redis 7.4 is the configured Docker/CI target; local Redis execution used 6.2.14.

Two upstream deprecation warnings were emitted by Starlette's HTTPX TestClient compatibility layer. They did not fail tests. Runtime API traffic uses HTTPX directly, independently of TestClient.

## Measured performance

Both experiments compare one serial slot with two worker processes containing four threads each. The same API adds a deliberate 20 ms delay to catalog operations. Each case also performs real HTTP setup and teardown, schema checks, and SQLite-backed fixture work.

One warmup round was discarded; three rounds were measured with alternating serial/distributed order. Timings include worker startup, coordination, fixture requests, validation, and response processing. They exclude final report writing and worker process teardown. This is a synthetic local workload, not production traffic.

| Workload | Serial median | Distributed median | Time reduction |
| --- | ---: | ---: | ---: |
| 60 cases | 2,515.692 ms | 2,490.666 ms | **0.99%** |
| 240 cases | 9,046.350 ms | 3,724.867 ms | **58.82%** |

The small run shows why a speedup cannot be promised: startup and coordination overhead can offset parallel work. The larger workload amortizes that overhead. Neither figure establishes the original resume's 20% claim for an unspecified production workload.

Raw measurements:

| Workload/mode | Round 1 (ms) | Round 2 (ms) | Round 3 (ms) |
| --- | ---: | ---: | ---: |
| 60 serial | 2,515.692 | 2,573.095 | 2,401.721 |
| 60 distributed | 2,603.306 | 2,490.666 | 2,184.213 |
| 240 serial | 9,046.350 | 9,127.503 | 8,823.721 |
| 240 distributed | 3,724.867 | 3,179.663 | 4,484.376 |

All cases passed in every measured and warmup benchmark round. These are small-sample descriptive measurements; confidence intervals and statistical significance were not estimated.

## Evidence files

- `evidence/summary.json`: machine-readable verification summary.
- `evidence/unit-tests.xml`: complete pytest JUnit results.
- `evidence/coverage.json`: statement and branch coverage details.
- `evidence/process-verification/verification.json`: process, recovery, and regression outcomes.
- `evidence/process-verification/distributed/`: acceptance reports and worker logs.
- `evidence/process-verification/broken-contract/`: reports proving the deliberate regression failed.
- `evidence/process-verification/benchmark/`: 60-case warmup, raw results, and timings.
- `evidence/throughput/`: 240-case warmup, raw results, and timings.

## Reproduce

```bash
uv sync --frozen --extra demo --group dev
uv run --frozen python scripts/dev_check.py --redis-server /absolute/path/to/redis-server
```

To repeat the larger benchmark against already running Redis and demo API processes:

```bash
uv run --frozen python scripts/benchmark.py \
  --suite examples/suites/throughput.json \
  --base-url http://127.0.0.1:8000 \
  --redis-url redis://127.0.0.1:6379/0 \
  --output reports/throughput
```

Start the demo API with `DEMO_DELAY_SECONDS=0.02`, enable test support, and set the same `TEST_SUPPORT_TOKEN` in the API and worker environment. Run timings will vary by hardware, runtime, and service behavior.
