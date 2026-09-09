def test_seed_idempotency_and_order_conflict(demo):
    headers = {"X-Test-Token": "test-secret-do-not-log"}
    url = "/test-support/namespaces/test"
    assert demo.put(url, headers=headers, json={"seed": 42}).status_code == 200
    before = demo.get("/catalog/test/products").json()
    assert demo.put(url, headers=headers, json={"seed": 42}).status_code == 200
    assert demo.get("/catalog/test/products").json() == before
    assert demo.put(url, headers=headers, json={"seed": 43}).status_code == 409
    body = {"id": "o", "product_id": "product-1", "quantity": 2}
    first = demo.post("/orders/test", json=body)
    assert first.status_code == 201
    assert demo.post("/orders/test", json=body).json() == first.json()
    assert demo.post("/orders/test", json={**body, "quantity": 3}).status_code == 409
    assert demo.delete(url, headers=headers).status_code == 204
    assert demo.delete(url, headers=headers).status_code == 204
    assert demo.get("/catalog/test/products").json() == {"items": []}


def test_test_support_disabled_and_requires_token(demo, monkeypatch):
    assert demo.put("/test-support/namespaces/n", json={"seed": 42}).status_code == 403
    monkeypatch.setenv("DEMO_ENABLE_TEST_SUPPORT", "false")
    assert demo.put("/test-support/namespaces/n", json={"seed": 42}).status_code == 404
