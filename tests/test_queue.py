from concurrent.futures import ThreadPoolExecutor

import pytest

from api_sentinel.models import CaseResult
from api_sentinel.queue import RunQueue


def result(job):
    return CaseResult(
        case_id=job.case.id, worker_id="worker", status="passed", delivery=job.delivery
    )


def test_unique_claims_atomic_completion_and_ttl(queue, suite, document):
    queue.create(suite, document)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda _: queue.claim(30), range(len(suite.cases))))
    assert len({job.case.id for job in jobs}) == len(suite.cases)
    assert queue.claim(30) is None
    for job in jobs:
        assert queue.complete(job, result(job))
        assert not queue.complete(job, result(job))
    assert queue.count() == len(suite.cases)
    assert all(queue.client.pttl(key) > 0 for key in queue.keys if queue.client.exists(key))


def test_expired_job_reclaimed_and_stale_worker_fenced(queue, suite, document):
    queue.create(suite, document)
    old = queue.claim(30)
    queue.client.zadd(queue.keys[3], {old.case.id: 0})
    assert not queue.renew(old, 30)
    assert not queue.complete(old, result(old))
    new = queue.claim(30)
    assert new.case.id == old.case.id and new.delivery == 2 and new.token != old.token
    assert not queue.complete(old, result(old))
    assert queue.complete(new, result(new))
    assert len(queue.results()) == 1


def test_lease_renewal(queue, suite, document):
    queue.create(suite, document)
    job = queue.claim(10)
    before = queue.client.zscore(queue.keys[3], job.case.id)
    assert queue.renew(job, 60)
    assert queue.client.zscore(queue.keys[3], job.case.id) > before


def test_delivery_limit_is_terminal_error(queue, suite, document):
    suite.cases = suite.cases[:1]
    queue.create(suite, document, max_deliveries=1)
    job = queue.claim(30)
    queue.client.zadd(queue.keys[3], {job.case.id: 0})
    assert queue.claim(30) is None
    assert queue.results()[0].status == "error"


def test_closed_run_rejects_late_claims_and_results(queue, suite, document):
    queue.create(suite, document)
    job = queue.claim(30)
    queue.close()
    assert not queue.is_open()
    assert queue.claim(30) is None
    assert not queue.renew(job, 30)
    assert not queue.complete(job, result(job))


def test_duplicate_run_and_replay_safety(queue, suite, document):
    suite.cases[0].replay_safe = False
    with pytest.raises(ValueError):
        queue.create(suite, document)
    suite.cases[0].replay_safe = True
    queue.create(suite, document)
    with pytest.raises(ValueError):
        queue.create(suite, document)


def test_deadline_stops_claims(queue, suite, document):
    queue.create(suite, document)
    queue.client.hset(queue.keys[0], "deadline", 0)
    assert not queue.is_open()
    assert queue.claim(30) is None


def test_result_identity_cannot_be_swapped(queue, suite, document):
    queue.create(suite, document)
    job = queue.claim(30)
    wrong = result(job)
    wrong.case_id = "someone-else"
    with pytest.raises(ValueError):
        queue.complete(job, wrong)


def test_invalid_run_id(queue):
    with pytest.raises(ValueError):
        RunQueue(queue.client, "bad{hash-tag}")


def test_plan_roundtrip_preserves_empty_arrays(queue, suite, document):
    assert queue.plan() is None
    queue.create(suite, document)
    assert queue.plan()["suite"] == suite.model_dump()
    job = queue.claim(30)
    assert isinstance(job.case.teardown, list)
