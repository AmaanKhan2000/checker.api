# Operating guide

## Deployment boundary

Run workers on trusted build agents with network access only to authorized test targets and Redis. The demo uses an internal Docker network with no published ports, opt-in test-support endpoints, a non-root application UID, read-only application filesystems, dropped capabilities, and CPU/memory limits. It is a local demonstration topology, not a managed high-availability deployment.

Use a dedicated Redis instance with authentication, TLS (`rediss://`), a non-evicting policy, monitored memory/disk, and a persistence policy matching your loss tolerance. The Compose sample enables AOF with `appendfsync everysec`; a host crash can still lose recently accepted writes. Configure stricter durability or repeat the entire CI run after Redis data loss. Redis is the run coordination authority and a single-primary availability dependency.

Use environment variables or your CI secret manager for Redis credentials and API tokens. Prefer `SENTINEL_REDIS_URL` over placing a credential-bearing URL directly in command arguments. Never commit a real `.env` file. `trust_env=False` disables implicit proxy and certificate environment configuration in HTTPX; HTTPS uses its certificate trust defaults. Private-CA/mTLS and explicit proxy configuration require extending the client options before use.

Container base images use named Python/Redis tags for accessibility. Before an organizational release, resolve and pin approved image digests, scan the resulting image, and keep those approvals current. Python application dependencies and transitive hashes are fixed in `uv.lock`.

## Capacity and timing

Start with two workers, four threads each, a 30-second lease, a 5-second request timeout, and a 60-second case budget. Add concurrency only after checking API latency, rate limits, Redis utilization, and test-data contention. More threads can increase contention or make tests slower.

Heartbeats run approximately every lease/3. Keep the lease comfortably above Redis round-trip stalls and expected process scheduling pauses. CLI allows shorter leases for fault testing; subsecond leases are inappropriate for typical production networks.

Case deadlines are cooperative. HTTPX timeouts govern individual blocking network phases; an in-flight read may return after the cooperative deadline by up to a request timeout. Cleanup gets a separate budget of at most 15 seconds. A coordinator's run deadline blocks late results even if an underlying thread is still finishing. Use an external container/job deadline when a strict process-level wall-clock cutoff is required. Choose a termination grace period above the case, request, and cleanup budgets.

Responses are streamed and checked against a 2 MB decoded-size limit before JSON validation. This limits retained payloads; it is not a complete memory sandbox for decompression or pathological JSON/schema inputs. This runner is intended for trusted test definitions against controlled services.

## Cleanup and retention

Normal teardown deletes each attempt's fixture namespace. A SIGKILL may leave the abandoned attempt's fixtures behind. Each attempt has a separate namespace, preventing stale attempts from deleting a recovering attempt's fixtures. Add an environment-specific age-based janitor for shared staging data. The demo database records namespace creation times, and `docker_demo.sh` removes its entire isolated volume at exit.

Redis run data expires at the run deadline plus 24 hours. Successful completion does not extend retention. Integration scripts delete only their explicitly allocated random run keys. They never issue FLUSHDB or FLUSHALL.

Store reports in your CI artifact system with the retention policy appropriate to the organization. Reports omit HTTP payloads, URLs, and sensitive exception messages, but suite names, operation labels, and case IDs remain visible.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Exit 1 | Read failed step entries in `results.json` or JUnit. Check response schema, status, JSON assertion, and latency budget. |
| Exit 2 before reports exist | Confirm configuration paths, supported OpenAPI subset, unique run ID, Redis connectivity, and output-directory permissions. Validation diagnostics intentionally avoid echoing input values. |
| All cases missing | Confirm workers use the same run ID, Redis URL, and target URL; inspect worker stderr logs. |
| Worker wait deadline | Coordinator did not submit the plan in time, or used a different Redis database/run ID. |
| Delivery count exceeds 1 | Worker crashed, lost its lease, or Redis communication interrupted. Check heartbeat and queue events. |
| `accepted: false` | Result arrived after lease/run expiry or ownership changed. Inspect the later accepted result. |
| Setup 403 | Worker test-support token is missing or differs from the demo API token. |
| Setup 404 | Demo test-support routes are disabled; enable only in the test environment. |
| Docker volume permissions | Keep named volumes or provision writable bind mounts for UID/GID 10001. |
| Poor benchmark result | Compare startup overhead, API latency, SQLite contention, CPU quotas, connection limits, and workload size. |

## CI integration

The workflow fails on lint, coverage, unit/integration failures, an unexpected passing regression, or a failing contract run. A release job depends on all gates. Configure your actual deployment job with `needs: release-gate` and configure branch protection separately. GitHub branch protection and deployment credentials are repository settings, not features that can be activated by downloading this project.

The workflow deliberately tests the failure path by enabling `DEMO_BREAK_CONTRACT=true`, which changes a product's integer price into a string. That run must return exactly 1; an infrastructure error returning 2 also fails the regression-gate check.
