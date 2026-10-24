"""Steady background traffic for the lab shop service.

Environment variables:
  TARGET_URL  base URL of the service (default http://app:8000)
  RPS         requests per second (default 20)
  ORDER_SHARE share of requests that create an order (default 0.1)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections import Counter

import httpx

TARGET_URL = os.environ.get("TARGET_URL", "http://app:8000")
RPS = float(os.environ.get("RPS", "20"))
ORDER_SHARE = float(os.environ.get("ORDER_SHARE", "0.1"))
REPORT_EVERY = 10.0


async def one_request(client: httpx.AsyncClient, stats: Counter[str]) -> None:
    try:
        if random.random() < ORDER_SHARE:
            response = await client.post("/orders")
        else:
            # Skewed popularity, so the cache has hot keys like a real shop.
            item_id = min(int(random.paretovariate(1.2)), 100)
            response = await client.get(f"/items/{item_id}")
        stats[str(response.status_code)] += 1
    except httpx.HTTPError as exc:
        stats[type(exc).__name__] += 1


async def main() -> None:
    stats: Counter[str] = Counter()
    interval = 1.0 / RPS
    last_report = time.monotonic()
    tasks: set[asyncio.Task[None]] = set()
    async with httpx.AsyncClient(base_url=TARGET_URL, timeout=5.0) as client:
        while True:
            task = asyncio.create_task(one_request(client, stats))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            await asyncio.sleep(interval)
            now = time.monotonic()
            if now - last_report >= REPORT_EVERY:
                print(json.dumps({"msg": "load_summary", "rps": RPS, "results": dict(stats)}))
                stats.clear()
                last_report = now


if __name__ == "__main__":
    asyncio.run(main())
