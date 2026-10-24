from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from shop.main import DependencyError, Settings, create_app


class FakeDatabase:
    def __init__(self) -> None:
        self.items = {1: {"id": 1, "name": "item-1", "price_cents": 107}}
        self.orders = 0
        self.down = False

    async def fetch_item(self, item_id: int) -> dict[str, Any] | None:
        if self.down:
            raise DependencyError("postgres", ConnectionError("refused"))
        return self.items.get(item_id)

    async def create_order(self, item_id: int, quantity: int) -> int:
        if self.down:
            raise DependencyError("postgres", ConnectionError("refused"))
        self.orders += 1
        return self.orders

    async def ping(self) -> None:
        if self.down:
            raise DependencyError("postgres", ConnectionError("refused"))


class FakeCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.down = False

    async def get(self, key: str) -> str | None:
        if self.down:
            raise DependencyError("redis", ConnectionError("refused"))
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        if self.down:
            raise DependencyError("redis", ConnectionError("refused"))
        self.data[key] = value

    async def ping(self) -> None:
        if self.down:
            raise DependencyError("redis", ConnectionError("refused"))


def make_client(mode: str = "fail") -> tuple[TestClient, FakeDatabase, FakeCache]:
    db, cache = FakeDatabase(), FakeCache()
    settings = Settings(
        database_url="unused", redis_url="unused", db_pool_size=1, cache_failure_mode=mode
    )
    app = create_app(settings=settings, db=db, cache=cache)
    return TestClient(app), db, cache


def test_item_is_cached_after_first_read() -> None:
    client, _, cache = make_client()
    response = client.get("/items/1")
    assert response.status_code == 200
    assert response.json()["name"] == "item-1"
    assert "item:1" in cache.data


def test_missing_item_is_404() -> None:
    client, _, _ = make_client()
    assert client.get("/items/999").status_code == 404


def test_redis_down_fail_mode_returns_503() -> None:
    client, _, cache = make_client("fail")
    cache.down = True
    response = client.get("/items/1")
    assert response.status_code == 503
    assert response.json() == {"error": "redis unavailable"}


def test_redis_down_degrade_mode_falls_back_to_db() -> None:
    client, _, cache = make_client("degrade")
    cache.down = True
    assert client.get("/items/1").status_code == 200


def test_readyz_reflects_dependencies() -> None:
    client, db, _ = make_client()
    assert client.get("/readyz").status_code == 200
    db.down = True
    assert client.get("/readyz").status_code == 503


def test_orders_created() -> None:
    client, db, _ = make_client()
    response = client.post("/orders")
    assert response.status_code == 201
    assert db.orders == 1


def test_metrics_count_requests_by_route_template() -> None:
    client, _, _ = make_client()
    client.get("/items/1")
    body = client.get("/metrics").text
    assert 'route="/items/{item_id}"' in body
    assert "shop_http_request_duration_seconds_bucket" in body


def test_invalid_cache_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_FAILURE_MODE", "sometimes")
    with pytest.raises(ValueError):
        Settings.from_env()
