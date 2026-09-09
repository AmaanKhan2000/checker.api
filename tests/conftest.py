import os
import uuid
from pathlib import Path

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from api_sentinel.config import load_document, load_suite
from api_sentinel.contracts import Contract
from api_sentinel.queue import RunQueue, connect

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def document():
    return load_document(ROOT / "examples/contract.json")


@pytest.fixture
def contract(document):
    return Contract(document)


@pytest.fixture
def suite():
    return load_suite(ROOT / "examples/suites/smoke.json")


@pytest.fixture(params=["emulated", "real"])
def queue(request):
    if request.param == "real":
        url = os.getenv("TEST_REDIS_URL")
        if not url:
            pytest.skip("TEST_REDIS_URL is not set; real Redis integration is opt-in")
        client = connect(url)
    else:
        client = fakeredis.FakeRedis(decode_responses=True)
    value = RunQueue(client, "test-" + uuid.uuid4().hex)
    yield value
    value.delete()
    client.close()


@pytest.fixture
def demo(monkeypatch, tmp_path):
    from examples.demo_api import app as module

    monkeypatch.setattr(module, "DB_PATH", str(tmp_path / "demo.sqlite3"))
    monkeypatch.setattr(module, "DELAY", 0)
    monkeypatch.setenv("DEMO_ENABLE_TEST_SUPPORT", "true")
    monkeypatch.setenv("TEST_SUPPORT_TOKEN", "test-secret-do-not-log")
    with TestClient(module.app) as client:
        yield client


@pytest.fixture
def transport(demo):
    def handle(request):
        result = demo.request(
            request.method,
            request.url.raw_path.decode(),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            result.status_code, headers=dict(result.headers), content=result.content
        )

    return httpx.MockTransport(handle)
