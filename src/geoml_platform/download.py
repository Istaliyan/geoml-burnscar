"""Stream downloads with bounded retries and optional STAC file integrity metadata."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from geoml_platform.storage import sha256_file


def expected_sha256(checksum: str | None) -> str | None:
    if checksum is None:
        return None
    # STAC file:checksum uses multihash: 0x12 (SHA-256), 0x20 (32 bytes).
    if len(checksum) != 68 or not checksum.startswith("1220"):
        raise ValueError("Only SHA-256 STAC file:checksum multihashes are supported")
    try:
        bytes.fromhex(checksum)
    except ValueError:
        raise ValueError("Invalid STAC file:checksum") from None
    return checksum[4:].lower()


def download(
    client: httpx.Client,
    href: str,
    target: Path,
    *,
    expected_size: int | None = None,
    expected_hash: str | None = None,
    attempts: int = 3,
    retry_delay: float = 1,
) -> tuple[str, int]:
    if urlsplit(href).scheme != "https":
        raise ValueError("Source assets must use HTTPS")
    for attempt in range(attempts):
        try:
            signed = href
            # SAS credentials are obtained per attempt and never persisted or logged.
            if (urlsplit(href).hostname or "").endswith(".blob.core.windows.net"):
                response = client.get(
                    "https://planetarycomputer.microsoft.com/api/sas/v1/sign",
                    params={"href": href},
                )
                response.raise_for_status()
                signed = response.json()["href"]
            with client.stream("GET", signed, headers={"Accept-Encoding": "identity"}) as stream:
                stream.raise_for_status()
                with target.open("wb") as output:
                    for chunk in stream.iter_bytes(chunk_size=1024 * 1024):
                        output.write(chunk)
                size = target.stat().st_size
                length = stream.headers.get("Content-Length")
                if size == 0 or (length is not None and size != int(length)):
                    raise ValueError("Downloaded size differs from Content-Length or is empty")
                if expected_size is not None and size != expected_size:
                    raise ValueError("Downloaded size differs from STAC file:size")
            digest = sha256_file(target)
            if expected_hash is not None and digest != expected_hash:
                raise ValueError("Downloaded checksum differs from STAC file:checksum")
            return digest, size
        except (httpx.HTTPError, ValueError) as error:
            target.unlink(missing_ok=True)
            status = (
                error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            )
            retryable = status is None or status in {403, 408, 429} or status >= 500
            if attempt + 1 == attempts or not retryable:
                # HTTP exception strings can expose SAS query parameters.
                raise ValueError(f"Asset download failed after {attempt + 1} attempt(s)") from None
            time.sleep(retry_delay * 2**attempt)
    raise ValueError("attempts must be positive")
