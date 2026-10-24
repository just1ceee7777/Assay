"""Minimal read-only HTTP client. Assay only ever sends GET requests."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 5.0

# Errors that mean "could not talk to the server at all".
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)


class SourceError(Exception):
    """A data source could not answer a query."""


def get(url: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status: int = response.status
            body: bytes = response.read()
            return status, body
    except urllib.error.HTTPError as exc:
        # The server answered, just not with 2xx: report its status, not "unreachable".
        return exc.code, exc.read()


def get_json(
    base_url: str, path: str, params: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        status, body = get(url, timeout)
    except NETWORK_ERRORS as exc:
        raise SourceError(f"{base_url} unreachable: {exc}") from exc
    if status != 200:
        reason = body.decode("utf-8", "replace").strip()[:200]
        raise SourceError(f"GET {path} -> HTTP {status}: {reason}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceError(f"GET {path}: response is not JSON") from exc
