"""Read-only checks that the telemetry sources Assay depends on are reachable.

Every check only sends GET requests. Nothing here changes the observed system.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_TIMEOUT = 5.0


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _get(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status: int = response.status
            body: bytes = response.read()
            return status, body
    except urllib.error.HTTPError as exc:
        # The server answered, just not with 2xx: report its status, not "unreachable".
        return exc.code, exc.read()


def check_ready(name: str, url: str, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """A service is ready when its readiness endpoint answers HTTP 200."""
    try:
        status, _ = _get(url, timeout)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return CheckResult(name, False, f"unreachable: {exc}")
    if status != 200:
        return CheckResult(name, False, f"HTTP {status}")
    return CheckResult(name, True, "ready")


def check_target_up(prometheus_url: str, job: str, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Prometheus scrapes the given job and every target of it reports up == 1."""
    name = f"prometheus target '{job}'"
    query = urllib.parse.urlencode({"query": f'up{{job="{job}"}}'})
    url = f"{prometheus_url.rstrip('/')}/api/v1/query?{query}"
    try:
        status, body = _get(url, timeout)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
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
