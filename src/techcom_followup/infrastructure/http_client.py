"""Constrained outbound HTTP with bounded responses."""

import asyncio
import json
import zlib
from datetime import datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx

MAX_RESPONSE_BYTES = 2_000_000
# Ten thousand task records with full notes; never for acknowledgements.
MAX_SOURCE_RESPONSE_BYTES = 32_000_000
REQUEST_DEADLINE_SECONDS = 10
# Only encodings this module can decode under its own output bound.
ACCEPT_ENCODING = "gzip, deflate"
_ZLIB_OR_GZIP = zlib.MAX_WBITS | 32


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
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        """Reject credentials, paths and unapproved origins.

        ``max_response_bytes`` bounds every decoded body; a task listing
        needs more room than an acknowledgement.
        """
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
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= MAX_SOURCE_RESPONSE_BYTES
        ):
            message = "response bound must be in 1..32000000 bytes"
            raise ValueError(message)
        self._client = client
        self._base = str(url).rstrip("/")
        self._limit = max_response_bytes

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
        headers = {"Accept-Encoding": ACCEPT_ENCODING}
        if operation_id is not None:
            headers["Idempotency-Key"] = operation_id
        async with self._client.stream(
            method,
            self._base + path,
            json=payload,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(5, connect=2),
        ) as response:
            content = await _read_bounded(response, self._limit)
            decoded_headers = response.headers.copy()
            # Drop wire metadata: the body is already decoded. Avoid
            # decoding again or retaining the compressed length.
            for name in (
                "Content-Encoding",
                "Content-Length",
                "Transfer-Encoding",
            ):
                decoded_headers.pop(name, None)
            return httpx.Response(
                response.status_code,
                headers=decoded_headers,
                content=content,
            )


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    if response.is_stream_consumed:
        # An in-process transport already read and decoded the body.
        body = _BoundedBody(None, limit)
        body.feed(response.content)
        return body.finish()
    body = _BoundedBody(response.headers.get("Content-Encoding"), limit)
    try:
        async for chunk in response.aiter_raw():
            body.feed(chunk)
        return body.finish()
    except zlib.error as error:
        msg = "response_encoding"
        raise UpstreamError(msg) from error


class _BoundedBody:
    """Decode a response body without ever exceeding the byte bound.

    A decoder asked for a whole compressed chunk at once could expand a
    small chunk a thousandfold before any size check; here every
    decompression step is capped at the remaining allowance.
    """

    def __init__(self, encoding: str | None, limit: int) -> None:
        name = (encoding or "identity").strip().lower()
        self._limit = limit
        self._decoder: zlib._Decompress | None = None
        if name in {"gzip", "x-gzip", "deflate"}:
            self._decoder = zlib.decompressobj(_ZLIB_OR_GZIP)
        elif name not in {"", "identity"}:
            msg = "response_encoding"
            raise UpstreamError(msg)
        self._content = bytearray()

    def feed(self, chunk: bytes) -> None:
        """Append one wire chunk, decoded under the remaining bound."""
        if self._decoder is None:
            self._append(chunk)
            return
        pending = chunk
        while pending:
            self._append(self._decoder.decompress(pending, self._room()))
            pending = self._decoder.unconsumed_tail

    def finish(self) -> bytes:
        """Return the decoded body after the last wire chunk."""
        if self._decoder is not None:
            self._append(self._decoder.flush())
        return bytes(self._content)

    def _room(self) -> int:
        # One byte beyond the bound detects an oversized body.
        return self._limit - len(self._content) + 1

    def _append(self, data: bytes) -> None:
        if len(self._content) + len(data) > self._limit:
            msg = "response_size"
            raise UpstreamError(msg)
        self._content.extend(data)


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
