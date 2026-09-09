"""Runnable inventory/order service; test-support routes are explicitly opt-in."""

import hmac
import json
import os
import random
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

DB_PATH = os.getenv("DEMO_DB_PATH", "/tmp/sentinel-demo.sqlite3")
DELAY = float(os.getenv("DEMO_DELAY_SECONDS", "0"))


@contextmanager
def database():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app):
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with database() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS namespaces (
          id TEXT PRIMARY KEY, seed INTEGER NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS products (
          namespace TEXT REFERENCES namespaces(id) ON DELETE CASCADE,
          id TEXT NOT NULL, name TEXT NOT NULL, price_cents INTEGER NOT NULL,
          PRIMARY KEY(namespace, id));
        CREATE TABLE IF NOT EXISTS orders (
          namespace TEXT REFERENCES namespaces(id) ON DELETE CASCADE,
          id TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL,
          PRIMARY KEY(namespace, id));
        """)
    yield


app = FastAPI(title="API Sentinel Demo", version="1.0.0", lifespan=lifespan)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeedRequest(StrictModel):
    seed: int


class OrderRequest(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9-]{1,50}$")
    product_id: str
    quantity: int = Field(ge=1, le=10)


def require_test_support(token: str | None):
    expected = os.getenv("TEST_SUPPORT_TOKEN", "")
    if os.getenv("DEMO_ENABLE_TEST_SUPPORT") != "true":
        raise HTTPException(404, "not found")
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(403, "forbidden")


def delay():
    if DELAY > 0:
        time.sleep(DELAY)


@app.get("/health", operation_id="health")
def health():
    with database() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.put("/test-support/namespaces/{namespace}", operation_id="seedNamespace")
def seed_namespace(
    namespace: str, body: SeedRequest, x_test_token: Annotated[str | None, Header()] = None
):
    require_test_support(x_test_token)
    rng = random.Random(body.seed)
    with database() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO namespaces VALUES (?, ?, ?)", (namespace, body.seed, time.time())
        )
        existing = conn.execute("SELECT seed FROM namespaces WHERE id=?", (namespace,)).fetchone()
        if existing["seed"] != body.seed:
            raise HTTPException(409, "namespace already has a different seed")
        for index, name in enumerate(("Phone", "Tablet", "Watch"), start=1):
            conn.execute(
                "INSERT OR IGNORE INTO products VALUES (?, ?, ?, ?)",
                (namespace, f"product-{index}", name, rng.randint(1000, 9999)),
            )
    return {"namespace": namespace, "seed": body.seed, "products": 3}


@app.delete("/test-support/namespaces/{namespace}", status_code=204, operation_id="deleteNamespace")
def delete_namespace(namespace: str, x_test_token: Annotated[str | None, Header()] = None):
    require_test_support(x_test_token)
    with database() as conn:
        conn.execute("DELETE FROM namespaces WHERE id=?", (namespace,))
    return Response(status_code=204)


@app.get("/catalog/{namespace}/products", operation_id="listProducts")
def list_products(namespace: str, limit: Annotated[int, Query(ge=1, le=100)] = 10):
    delay()
    with database() as conn:
        rows = conn.execute(
            "SELECT id, name, price_cents FROM products WHERE namespace=? ORDER BY id LIMIT ?",
            (namespace, limit),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/catalog/{namespace}/products/{product_id}", operation_id="getProduct")
def get_product(namespace: str, product_id: str):
    delay()
    with database() as conn:
        row = conn.execute(
            "SELECT id, name, price_cents FROM products WHERE namespace=? AND id=?",
            (namespace, product_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "product not found")
    result = dict(row)
    if os.getenv("DEMO_BREAK_CONTRACT") == "true":
        result["price_cents"] = "broken-type"
    return result


@app.post("/orders/{namespace}", status_code=201, operation_id="createOrder")
def create_order(namespace: str, body: OrderRequest):
    delay()
    serialized = body.model_dump_json()
    with database() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT request, response FROM orders WHERE namespace=? AND id=?", (namespace, body.id)
        ).fetchone()
        if previous:
            if previous["request"] != serialized:
                raise HTTPException(409, "order ID conflicts with an existing request")
            return json.loads(previous["response"])
        product = conn.execute(
            "SELECT price_cents FROM products WHERE namespace=? AND id=?",
            (namespace, body.product_id),
        ).fetchone()
        if product is None:
            raise HTTPException(404, "product not found")
        result = {
            "id": body.id,
            "product_id": body.product_id,
            "quantity": body.quantity,
            "total_cents": product["price_cents"] * body.quantity,
            "status": "confirmed",
        }
        conn.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?)",
            (namespace, body.id, serialized, json.dumps(result)),
        )
    return result
