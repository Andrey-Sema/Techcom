"""Constrained outbound HTTP with bounded responses."""

import asyncio
import json
from datetime import datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx

MAX_RESPONSE_BYTES = 2_000_000
REQUEST_DEADLINE_SECONDS = 10


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
        """Reject credentials, paths and unapproved origins."""
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
        """Bound response bytes and disable redirects."""
        async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
            return await self._request(
                method, path, payload=payload, operation_id=operation_id
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: object,
        operation_id: str | None,
    ) -> httpx.Response:
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
            decoded_headers = response.headers.copy()
            # Drop wire metadata: aiter_bytes already decoded the body.
            # Avoid decoding again or retaining the compressed length.
            for name in (
                "Content-Encoding",
                "Content-Length",
                "Transfer-Encoding",
            ):
                decoded_headers.pop(name, None)
            return httpx.Response(
                response.status_code,
                headers=decoded_headers,
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
    """Parse finite, capped seconds or an HTTP-date."""
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
