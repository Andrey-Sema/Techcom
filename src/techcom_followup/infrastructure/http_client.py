"""Constrained outbound HTTP transport with bounded streamed."""

import json
from datetime import datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx

MAX_RESPONSE_BYTES = 2_000_000


class UpstreamError(Exception):
    """Closed upstream error code; never include response bodies."""


class HttpEndpoint:
    """Bind one configured origin and an explicit host allow-list."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        allowed_hosts: frozenset[str],
        allow_local_http: bool = False,
    ) -> None:
        """Reject redirects, credentials, paths and unapproved."""
        url = httpx.URL(base_url)
        local_http = allow_local_http and url.host in {
            "mock-api",
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if (
            url.host not in allowed_hosts
            or url.scheme
            not in ({"https", "http"} if local_http else {"https"})
            or url.userinfo
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            message = "outbound origin is not allowed"
            raise ValueError(message)
        self._client = client
        self._base = str(url).rstrip("/")

    async def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        operation_id: str | None = None,
    ) -> httpx.Response:
        """Read no more than the response cap with redirects."""
        headers = (
            {} if operation_id is None else {"Idempotency-Key": operation_id}
        )
        async with self._client.stream(
            method,
            self._base + path,
            json=payload,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(5, connect=2),
        ) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    msg = "response_size"
                    raise UpstreamError(msg)
                content.extend(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(content),
            )


def json_body(response: httpx.Response) -> object:
    """Decode JSON, translating parser internals into a closed code."""
    try:
        return json.loads(response.content)
    except (ValueError, UnicodeDecodeError) as error:
        msg = "invalid_json"
        raise UpstreamError(msg) from error


def retry_after(value: str | None, now: datetime) -> float:
    """Parse seconds or HTTP-date, rejecting non-finite and huge."""
    if value is None:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            seconds = (stamp - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return min(300, max(0, seconds)) if isfinite(seconds) else 0
