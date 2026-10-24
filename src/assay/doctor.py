"""Read-only checks that the telemetry sources Assay depends on are reachable.

Every check only sends GET requests. Nothing here changes the observed system.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass

from assay.http import DEFAULT_TIMEOUT, NETWORK_ERRORS
from assay.http import get as _get

MAX_REASON = 120


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_ready(name: str, url: str, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """A service is ready when its readiness endpoint answers HTTP 200."""
    try:
        status, body = _get(url, timeout)
    except NETWORK_ERRORS as exc:
        return CheckResult(name, False, f"unreachable: {exc}")
    if status != 200:
        # The body says why, e.g. Loki's "Ingester not ready" during its warm-up.
        reason = body.decode("utf-8", "replace").strip().splitlines()
        detail = f"HTTP {status}: {reason[0][:MAX_REASON]}" if reason else f"HTTP {status}"
        return CheckResult(name, False, detail)
    return CheckResult(name, True, "ready")


def check_target_up(prometheus_url: str, job: str, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Prometheus scrapes the given job and every target of it reports up == 1."""
    name = f"prometheus target '{job}'"
    query = urllib.parse.urlencode({"query": f'up{{job="{job}"}}'})
    url = f"{prometheus_url.rstrip('/')}/api/v1/query?{query}"
    try:
        status, body = _get(url, timeout)
    except NETWORK_ERRORS as exc:
        return CheckResult(name, False, f"unreachable: {exc}")
    if status != 200:
        return CheckResult(name, False, f"HTTP {status}")
    try:
        results = json.loads(body)["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return CheckResult(name, False, "unexpected response format")
    if not results:
        return CheckResult(name, False, "no targets found")
    down = [r["metric"].get("instance", "?") for r in results if r["value"][1] != "1"]
    if down:
        return CheckResult(name, False, f"down: {', '.join(down)}")
    return CheckResult(name, True, f"{len(results)} target(s) up")


def run_checks(prometheus_url: str, loki_url: str, job: str) -> list[CheckResult]:
    return [
        check_ready("prometheus", f"{prometheus_url.rstrip('/')}/-/ready"),
        check_ready("loki", f"{loki_url.rstrip('/')}/ready"),
        check_target_up(prometheus_url, job),
    ]
