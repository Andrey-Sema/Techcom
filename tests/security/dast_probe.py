"""Require authenticated fixture access before an isolated ZAP scan."""

import json
from pathlib import Path

import httpx


def main() -> None:
    properties = Path("/audit/auth.properties").read_text(encoding="utf-8")
    prefix = "replacer.full_list(0).replacement=Bearer "
    tokens = [
        line.removeprefix(prefix)
        for line in properties.splitlines()
        if line.startswith(prefix)
    ]
    if len(tokens) != 1:
        message = "missing isolated scan credential"
        raise SystemExit(message)
    try:
        with (
            httpx.Client(
                timeout=5, follow_redirects=False, trust_env=False
            ) as client,
            client.stream(
                "GET",
                "http://app:8080/v1/actions?limit=1",
                headers={"Authorization": f"Bearer {tokens[0]}"},
            ) as response,
        ):
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_raw(chunk_size=1024):
                raw.extend(chunk)
                if len(raw) > 65536:
                    message = "DAST preflight response exceeded bound"
                    raise SystemExit(message)
            payload = json.loads(raw)
            accepted = response.status_code == 200 and isinstance(
                payload, list
            )
    except (httpx.HTTPError, ValueError):
        message = "authenticated DAST preflight failed"
        raise SystemExit(message) from None
    if not accepted or not payload:
        message = "DAST requires an authenticated fixture response"
        raise SystemExit(message)


if __name__ == "__main__":
    main()
