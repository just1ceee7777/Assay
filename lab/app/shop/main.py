"""Small shop API backed by PostgreSQL with a Redis read-through cache.

Configuration (environment variables):
  DATABASE_URL        postgresql://user:password@host:5432/db
  REDIS_URL           redis://host:6379/0
  DB_POOL_SIZE        size of the PostgreSQL connection pool (default 5)
  CACHE_FAILURE_MODE  "fail": requests that need the cache return 503 when Redis is down;
                      "degrade": fall back to PostgreSQL (default "fail")
  LOG_LEVEL           default INFO
  APP_VERSION         reported as shop_build_info{version=...} (default "dev")
  RELATED_ITEMS_QUERY how item pages load their related items: "batch" (one query) or
                      "per_item" (one query per related item, the N+1 pattern)
                      (default "batch")

Fault behaviour is controlled from outside (stopping containers, network faults) or by
the flags above, never by code paths that know which scenario is running.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from shop.logging_setup import setup_logging

log = logging.getLogger("shop")

REQUESTS = Counter(
    "shop_http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
LATENCY = Histogram(
    "shop_http_request_duration_seconds",
    "HTTP request duration",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
DEPENDENCY_ERRORS = Counter(
    "shop_dependency_errors_total", "Errors talking to dependencies", ["dependency"]
)
CACHE_LOOKUPS = Counter("shop_cache_lookups_total", "Cache lookups", ["result"])
# Constant 1, labelled with the running version: a new label value marks a release.
BUILD_INFO = Gauge("shop_build_info", "Version of the running shop", ["version"])

ITEM_COUNT = 100
RELATED_COUNT = 20
RELATED_MODES = {"batch", "per_item"}
CACHE_TTL_SECONDS = 30


class DependencyError(Exception):
    def __init__(self, dependency: str, cause: BaseException) -> None:
        super().__init__(f"{dependency}: {cause}")
        self.dependency = dependency
        self.cause = cause


class Database(Protocol):
    async def fetch_item(self, item_id: int) -> dict[str, Any] | None: ...
    async def fetch_items(self, item_ids: list[int]) -> list[dict[str, Any]]: ...
    async def create_order(self, item_id: int, quantity: int) -> int: ...
    async def ping(self) -> None: ...


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int) -> None: ...
    async def ping(self) -> None: ...


@dataclass
class Settings:
    database_url: str
    redis_url: str
    db_pool_size: int
    cache_failure_mode: str
    version: str = "dev"
    related_items_query: str = "batch"

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("CACHE_FAILURE_MODE", "fail")
        if mode not in {"fail", "degrade"}:
            raise ValueError(f"CACHE_FAILURE_MODE must be fail or degrade, got {mode!r}")
        related = os.environ.get("RELATED_ITEMS_QUERY", "batch")
        if related not in RELATED_MODES:
            raise ValueError(f"RELATED_ITEMS_QUERY must be batch or per_item, got {related!r}")
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql://shop:shop@localhost:5432/shop"
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            db_pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
            cache_failure_mode=mode,
            version=os.environ.get("APP_VERSION", "dev"),
            related_items_query=related,
        )


class PostgresDatabase:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, url: str, pool_size: int) -> PostgresDatabase:
        import asyncpg

        pool = await asyncpg.create_pool(url, min_size=1, max_size=pool_size)
        db = cls(pool)
        await db._migrate()
        return db

    async def _migrate(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    price_cents INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    quantity INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            await conn.executemany(
                "INSERT INTO items (id, name, price_cents) VALUES ($1, $2, $3) "
                "ON CONFLICT (id) DO NOTHING",
                [(i, f"item-{i}", 100 + i * 7) for i in range(1, ITEM_COUNT + 1)],
            )

    async def fetch_item(self, item_id: int) -> dict[str, Any] | None:
        try:
            row = await self._pool.fetchrow(
                "SELECT id, name, price_cents FROM items WHERE id = $1", item_id
            )
        except Exception as exc:
            raise DependencyError("postgres", exc) from exc
        return dict(row) if row else None

    async def fetch_items(self, item_ids: list[int]) -> list[dict[str, Any]]:
        try:
            rows = await self._pool.fetch(
                "SELECT id, name, price_cents FROM items WHERE id = ANY($1::int[]) ORDER BY id",
                item_ids,
            )
        except Exception as exc:
            raise DependencyError("postgres", exc) from exc
        return [dict(row) for row in rows]

    async def create_order(self, item_id: int, quantity: int) -> int:
        try:
            order_id: int = await self._pool.fetchval(
                "INSERT INTO orders (item_id, quantity) VALUES ($1, $2) RETURNING id",
                item_id,
                quantity,
            )
        except Exception as exc:
            raise DependencyError("postgres", exc) from exc
        return order_id

    async def ping(self) -> None:
        try:
            await self._pool.fetchval("SELECT 1")
        except Exception as exc:
            raise DependencyError("postgres", exc) from exc

    async def close(self) -> None:
        await self._pool.close()


class RedisCache:
    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def connect(cls, url: str) -> RedisCache:
        import redis.asyncio as redis

        client = redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        return cls(client)

    async def get(self, key: str) -> str | None:
        try:
            value = await self._client.get(key)
        except Exception as exc:
            raise DependencyError("redis", exc) from exc
        return value.decode() if value is not None else None

    async def set(self, key: str, value: str, ttl: int) -> None:
        try:
            await self._client.set(key, value, ex=ttl)
        except Exception as exc:
            raise DependencyError("redis", exc) from exc

    async def ping(self) -> None:
        try:
            await self._client.ping()
        except Exception as exc:
            raise DependencyError("redis", exc) from exc

    async def close(self) -> None:
        await self._client.aclose()


def related_ids(item_id: int) -> list[int]:
    """The items shown next to `item_id`: its neighbours in the catalogue."""
    return [(item_id + k - 1) % ITEM_COUNT + 1 for k in range(1, RELATED_COUNT + 1)]


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    cache: Cache | None = None,
) -> FastAPI:
    """Build the app. Tests pass fake db/cache; production connects on startup."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: list[Any] = []
        if db is None:
            app.state.db = await PostgresDatabase.connect(
                settings.database_url, settings.db_pool_size
            )
            owned.append(app.state.db)
        if cache is None:
            app.state.cache = RedisCache.connect(settings.redis_url)
            owned.append(app.state.cache)
        log.info(
            "startup",
            extra={"cache_failure_mode": settings.cache_failure_mode, "version": settings.version},
        )
        yield
        for resource in owned:
            await resource.close()

    BUILD_INFO.labels(settings.version).set(1)
    app = FastAPI(title="shop", lifespan=lifespan)
    app.state.db = db
    app.state.cache = cache
    app.state.settings = settings

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            elapsed = time.perf_counter() - start
            if route_path != "/metrics":
                REQUESTS.labels(request.method, route_path, str(status)).inc()
                LATENCY.labels(route_path).observe(elapsed)
                log.info(
                    "request",
                    extra={
                        "method": request.method,
                        "route": route_path,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 2),
                    },
                )

    @app.exception_handler(DependencyError)
    async def dependency_error(request: Request, exc: DependencyError) -> JSONResponse:
        DEPENDENCY_ERRORS.labels(exc.dependency).inc()
        log.error(
            "dependency_error",
            extra={"dependency": exc.dependency, "error": repr(exc.cause)},
        )
        return JSONResponse(status_code=503, content={"error": f"{exc.dependency} unavailable"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, str]:
        await request.app.state.db.ping()
        await request.app.state.cache.ping()
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/items/{item_id}")
    async def get_item(item_id: int, request: Request) -> dict[str, Any]:
        state = request.app.state
        key = f"item:{item_id}"
        try:
            cached = await state.cache.get(key)
        except DependencyError:
            if state.settings.cache_failure_mode == "fail":
                raise
            CACHE_LOOKUPS.labels("error").inc()
            cached = None
        else:
            CACHE_LOOKUPS.labels("hit" if cached is not None else "miss").inc()
        if cached is not None:
            item: dict[str, Any] = json.loads(cached)
        else:
            row = await state.db.fetch_item(item_id)
            if row is None:
                raise HTTPException(status_code=404, detail="item not found")
            item = row
            try:
                await state.cache.set(key, json.dumps(item), CACHE_TTL_SECONDS)
            except DependencyError:
                if state.settings.cache_failure_mode == "fail":
                    raise
        # Related items are personalised in a real shop, so they are never cached.
        ids = related_ids(item_id)
        if state.settings.related_items_query == "batch":
            related = await state.db.fetch_items(ids)
        else:
            related = [r for i in ids if (r := await state.db.fetch_item(i)) is not None]
        return {**item, "related": [r["id"] for r in related]}

    @app.post("/orders", status_code=201)
    async def create_order(request: Request) -> dict[str, int]:
        item_id = random.randint(1, ITEM_COUNT)
        order_id = await request.app.state.db.create_order(item_id, 1)
        return {"order_id": order_id, "item_id": item_id}

    return app


def build() -> FastAPI:
    """Entry point for uvicorn: `uvicorn shop.main:build --factory`."""
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    return create_app()
