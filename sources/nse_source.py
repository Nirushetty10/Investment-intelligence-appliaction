"""
sources/nse_source.py — the ONLY module that talks to nseindia.com.

Discovery, parsing and normalization must never call `requests` directly;
they go through NSEClient so retry/backoff/rate-limiting/logging is
centralized and consistent (spec §34).

NOTE ON THIS ENVIRONMENT: this module is written to run against the real
NSE site. It cannot be exercised end-to-end inside the container this code
was generated in, because that container's network egress is restricted to
package registries (pypi/npm/github) and does not include nseindia.com.
Run it from an environment with normal internet access. The parser and
normalizer (which don't need live network access) are covered by the
fixture test in tests/, which exercises them against a saved real filing.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from config.settings import NSE, HTTP, RAW_STORE_DIR

logger = logging.getLogger("nse_pipeline.source")


class NSEFetchError(Exception):
    """Raised when a request to NSE ultimately fails after retries."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    content: bytes
    content_type: str
    response_timestamp: str
    content_hash: str
    request_params: Optional[dict] = None

    def save(self, subdir: str, filename: str) -> Path:
        target_dir = RAW_STORE_DIR / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / filename
        # Never overwrite existing raw content (spec §23) — if identical
        # content already exists under this name, this is a no-op; if the
        # name collides with *different* content, write a hash-suffixed copy.
        if path.exists():
            existing_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing_hash == self.content_hash:
                return path
            path = target_dir / f"{path.stem}.{self.content_hash[:12]}{path.suffix}"
        path.write_bytes(self.content)
        return path


class NSEClient:
    """
    Session-aware NSE HTTP client.

    NSE's web application requires a browser-like session: you must first
    GET the homepage (or another HTML page) to receive cookies before any
    /api/* call will succeed; calling the API cold returns 401/403.
    """

    def __init__(self, delay_seconds: float = None, max_retries: int = None):
        self.delay_seconds = delay_seconds if delay_seconds is not None else HTTP.default_delay_seconds
        self.max_retries = max_retries if max_retries is not None else HTTP.max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": HTTP.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._warmed_up = False
        self._last_request_time = 0.0

    def _warm_up(self):
        if self._warmed_up:
            return
        resp = self.session.get(NSE.base, timeout=HTTP.timeout_seconds)
        resp.raise_for_status()
        self._warmed_up = True
        time.sleep(self.delay_seconds)

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)

    def get(
        self,
        url: str,
        params: Optional[dict] = None,
        is_api: bool = True,
        accept_json: bool = True,
    ) -> FetchResult:
        """
        GET with retry + exponential backoff + jitter. Raises NSEFetchError
        only after exhausting retries; every attempt is logged so failures
        are diagnosable (spec §36 / §41), never silently swallowed.
        """
        if is_api:
            self._warm_up()
            headers = {"Referer": NSE.base, "X-Requested-With": "XMLHttpRequest"}
            if accept_json:
                headers["Accept"] = "application/json"
        else:
            headers = {}

        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=HTTP.timeout_seconds
                )
                self._last_request_time = time.time()

                if resp.status_code == 429:
                    wait = self._backoff_seconds(attempt)
                    logger.warning(
                        "NSE 429 rate-limited url=%s attempt=%d/%d wait=%.1fs",
                        url, attempt, self.max_retries, wait,
                    )
                    time.sleep(wait)
                    continue

                if 500 <= resp.status_code < 600:
                    wait = self._backoff_seconds(attempt)
                    logger.warning(
                        "NSE 5xx url=%s status=%d attempt=%d/%d wait=%.1fs",
                        url, resp.status_code, attempt, self.max_retries, wait,
                    )
                    time.sleep(wait)
                    continue

                if resp.status_code in (401, 403):
                    # session likely stale; re-warm and retry once
                    logger.warning("NSE %d url=%s — re-establishing session", resp.status_code, url)
                    self._warmed_up = False
                    self._warm_up()
                    continue

                resp.raise_for_status()

                content = resp.content
                return FetchResult(
                    url=url,
                    status_code=resp.status_code,
                    content=content,
                    content_type=resp.headers.get("Content-Type", ""),
                    response_timestamp=_now_iso(),
                    content_hash=hashlib.sha256(content).hexdigest(),
                    request_params=params,
                )

            except requests.RequestException as exc:
                last_exc = exc
                wait = self._backoff_seconds(attempt)
                logger.warning(
                    "NSE request error url=%s attempt=%d/%d error=%s wait=%.1fs",
                    url, attempt, self.max_retries, exc, wait,
                )
                time.sleep(wait)

        raise NSEFetchError(f"Failed to fetch {url} after {self.max_retries} attempts: {last_exc}")

    def _backoff_seconds(self, attempt: int) -> float:
        base = min(HTTP.backoff_base_seconds * (2 ** (attempt - 1)), HTTP.backoff_max_seconds)
        return base + random.uniform(0, 0.5 * base)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
