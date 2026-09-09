"""A single-primary Redis queue with atomic leases and fenced result acceptance.

Every key is run-scoped and shares a Redis Cluster hash tag. Redis TIME is the
authority for leases. This is at-least-once execution, not exactly-once side effects.
"""

import json
import re
import uuid
from dataclasses import dataclass

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from api_sentinel.models import Case, CaseResult, Suite

COMMON = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local function touch()
  local expires = redis.call('HGET', KEYS[1], 'expires')
  if expires then
    for _, key in ipairs(KEYS) do redis.call('PEXPIREAT', key, expires) end
  end
end
local function active()
  return redis.call('HGET', KEYS[1], 'state') == 'open'
    and tonumber(redis.call('HGET', KEYS[1], 'deadline') or '0') > now
end
"""

CREATE = (
    COMMON
    + """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local cases = cjson.decode(ARGV[2])
redis.call('HSET', KEYS[1], 'payload', ARGV[1], 'state', 'open',
  'deadline', now + tonumber(ARGV[3]), 'expires', now + tonumber(ARGV[3]) + tonumber(ARGV[4]),
  'deliveries', ARGV[5], 'total', #cases)
for i, case in ipairs(cases) do
  redis.call('HSET', KEYS[2], case.id, case.payload)
  redis.call('ZADD', KEYS[3], i, case.id)
end
touch()
return 1
"""
)

CLAIM = (
    COMMON
    + """
if not active() then return nil end
local expired = redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now, 'LIMIT', 0, 100)
for _, id in ipairs(expired) do
  redis.call('ZREM', KEYS[4], id)
  redis.call('HDEL', KEYS[5], id)
  if redis.call('HEXISTS', KEYS[7], id) == 0 then redis.call('ZADD', KEYS[3], 0, id) end
end
for _ = 1, 100 do
  local ready = redis.call('ZPOPMIN', KEYS[3], 1)
  if #ready == 0 then touch(); return nil end
  local id = ready[1]
  local attempt = redis.call('HINCRBY', KEYS[6], id, 1)
  if attempt > tonumber(redis.call('HGET', KEYS[1], 'deliveries')) then
    redis.call('HSET', KEYS[7], id, cjson.encode({case_id=id, status='error',
      worker_id='queue', delivery=attempt-1, message='worker delivery limit exhausted'}))
  else
    redis.call('HSET', KEYS[5], id, ARGV[1])
    redis.call('ZADD', KEYS[4], now + tonumber(ARGV[2]), id)
    touch()
    return {redis.call('HGET', KEYS[2], id), tostring(attempt)}
  end
end
touch()
return nil
"""
)

RENEW = (
    COMMON
    + """
if not active() then return 0 end
if redis.call('HGET', KEYS[5], ARGV[1]) ~= ARGV[2] then return 0 end
if tonumber(redis.call('ZSCORE', KEYS[4], ARGV[1]) or '0') <= now then return 0 end
redis.call('ZADD', KEYS[4], now + tonumber(ARGV[3]), ARGV[1])
touch()
return 1
"""
)

COMPLETE = (
    COMMON
    + """
if not active() then return 0 end
if redis.call('HGET', KEYS[5], ARGV[1]) ~= ARGV[2] then return 0 end
if tonumber(redis.call('ZSCORE', KEYS[4], ARGV[1]) or '0') <= now then return 0 end
if redis.call('HEXISTS', KEYS[7], ARGV[1]) == 1 then return 0 end
redis.call('HSET', KEYS[7], ARGV[1], ARGV[3])
redis.call('HDEL', KEYS[5], ARGV[1])
redis.call('ZREM', KEYS[4], ARGV[1])
touch()
return 1
"""
)

CLOSE = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'state', 'closed')
return 1
"""


@dataclass(frozen=True)
class Job:
    case: Case
    token: str
    delivery: int


def connect(url: str) -> Redis:
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=15,
        retry=Retry(NoBackoff(), 0),
    )


class RunQueue:
    def __init__(self, client: Redis, run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", run_id):
            raise ValueError("invalid run ID")
        self.client = client
        self.run_id = run_id
        self.keys = [
            f"sentinel:{{{run_id}}}:{suffix}"
            for suffix in ("meta", "tasks", "ready", "leases", "owners", "attempts", "results")
        ]
        self._create = client.register_script(CREATE)
        self._claim = client.register_script(CLAIM)
        self._renew = client.register_script(RENEW)
        self._complete = client.register_script(COMPLETE)
        self._close = client.register_script(CLOSE)

    def create(
        self,
        suite: Suite,
        contract: dict,
        timeout: float = 300,
        retention: int = 86400,
        max_deliveries: int = 3,
        base_url: str | None = None,
        executor_options: dict | None = None,
    ):
        if timeout <= 0 or retention < 1 or max_deliveries < 1:
            raise ValueError("invalid run timing or delivery settings")
        if any(not case.replay_safe for case in suite.cases):
            raise ValueError("distributed cases must explicitly set replay_safe: true")
        payload = json.dumps(
            {
                "suite": suite.model_dump(),
                "contract": contract,
                "base_url": base_url,
                "execution": executor_options or {},
            }
        )
        cases = json.dumps(
            [{"id": case.id, "payload": case.model_dump_json()} for case in suite.cases]
        )
        if len(payload.encode()) + len(cases.encode()) > 10_000_000:
            raise ValueError("distributed run payload exceeds 10 MB")
        created = self._create(
            keys=self.keys,
            args=[payload, cases, int(timeout * 1000), retention * 1000, max_deliveries],
        )
        if not created:
            raise ValueError("run ID already exists; use a new run ID")

    def plan(self) -> dict | None:
        payload = self.client.hget(self.keys[0], "payload")
        return json.loads(payload) if payload else None

    def is_open(self) -> bool:
        state, deadline = self.client.hmget(self.keys[0], "state", "deadline")
        seconds, micros = self.client.time()
        return state == "open" and float(deadline or 0) > seconds * 1000 + micros / 1000

    def claim(self, lease_seconds: float) -> Job | None:
        token = uuid.uuid4().hex
        raw = self._claim(keys=self.keys, args=[token, int(lease_seconds * 1000)])
        return Job(Case.model_validate_json(raw[0]), token, int(raw[1])) if raw else None

    def renew(self, job: Job, lease_seconds: float) -> bool:
        return bool(
            self._renew(keys=self.keys, args=[job.case.id, job.token, int(lease_seconds * 1000)])
        )

    def complete(self, job: Job, result: CaseResult) -> bool:
        if result.case_id != job.case.id:
            raise ValueError("result identity does not match the claimed case")
        return bool(
            self._complete(keys=self.keys, args=[job.case.id, job.token, result.model_dump_json()])
        )

    def results(self) -> list[CaseResult]:
        return [CaseResult.model_validate_json(value) for value in self.client.hvals(self.keys[6])]

    def count(self) -> int:
        return self.client.hlen(self.keys[6])

    def close(self):
        self._close(keys=self.keys, args=[])

    def delete(self):
        """Explicit run-scoped deletion, intended for isolated integration tests."""
        self.client.delete(*self.keys)
