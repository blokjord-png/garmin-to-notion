"""Notion client construction with rate limiting and automatic retries.

Notion allows an average of ~3 requests per second per integration and answers
with HTTP 429 ("You have been rate limited") when a burst exceeds that. The
stock notion-client does not retry, so a single 429 used to abort the whole
sync. The client built here does two things about that:

1. Throttle: it spaces consecutive requests at least NOTION_MIN_REQUEST_INTERVAL
   seconds apart (default 0.35s, i.e. just under 3 requests/second), so the
   scripts stay below the limit in the first place.
2. Retry: when Notion still answers 429 (or a transient 5xx / timeout), it
   waits for the duration Notion asks for in the Retry-After header (or an
   exponential backoff) and retries, up to NOTION_MAX_RETRIES times.
"""

import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from notion_client import Client
from notion_client.errors import HTTPResponseError, RequestTimeoutError

# Notion's documented limit is an average of 3 requests per second.
DEFAULT_MIN_REQUEST_INTERVAL = 0.35
DEFAULT_MAX_RETRIES = 6
DEFAULT_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class NotionDatabases:
    activities: str
    personal_records: str
    sleep: str
    daily_steps: str


class RetryingNotionClient(Client):
    """notion_client.Client that throttles requests and retries on 429/5xx."""

    def __init__(
        self,
        *args: Any,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep_fn=time.sleep,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._min_request_interval = max(0.0, min_request_interval)
        self._max_retries = max(0, max_retries)
        self._backoff_seconds = max(0.0, backoff_seconds)
        self._sleep = sleep_fn
        self._last_request_at: Optional[float] = None

    def _throttle(self) -> None:
        if self._last_request_at is not None and self._min_request_interval > 0:
            elapsed = time.monotonic() - self._last_request_at
            wait = self._min_request_interval - elapsed
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _retry_after_seconds(error: HTTPResponseError) -> Optional[float]:
        raw = error.headers.get("Retry-After") if error.headers is not None else None
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def _delay_for_attempt(self, attempt: int, error: Exception) -> float:
        # Prefer the wait Notion asks for; otherwise back off exponentially.
        if isinstance(error, HTTPResponseError):
            retry_after = self._retry_after_seconds(error)
            if retry_after is not None:
                return min(retry_after, MAX_BACKOFF_SECONDS)
        return min(self._backoff_seconds * (2 ** attempt), MAX_BACKOFF_SECONDS)

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        if isinstance(error, RequestTimeoutError):
            return True
        if isinstance(error, HTTPResponseError):
            return error.status in RETRYABLE_STATUS_CODES
        return False

    def request(self, *args: Any, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            self._throttle()
            try:
                return super().request(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 - we re-raise below
                if not self._is_retryable(error) or attempt >= self._max_retries:
                    raise
                delay = self._delay_for_attempt(attempt, error)
                attempt += 1
                status = getattr(error, "status", "timeout")
                print(
                    f"Notion API responded with {status}; retrying in "
                    f"{delay:.1f}s (attempt {attempt}/{self._max_retries})...",
                    flush=True,
                )
                self._sleep(delay)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def get_notion_client() -> tuple[Client, NotionDatabases]:
    print("Initializing Notion client...")

    notion_databases = NotionDatabases(
        activities=os.getenv("NOTION_DB_ID"),
        personal_records=os.getenv("NOTION_PR_DB_ID"),
        sleep=os.getenv("NOTION_SLEEP_DB_ID"),
        daily_steps=os.getenv("NOTION_STEPS_DB_ID"),
    )

    notion_token = os.getenv("NOTION_TOKEN")
    notion_client = RetryingNotionClient(
        auth=notion_token,
        min_request_interval=_float_env(
            "NOTION_MIN_REQUEST_INTERVAL", DEFAULT_MIN_REQUEST_INTERVAL
        ),
        max_retries=_int_env("NOTION_MAX_RETRIES", DEFAULT_MAX_RETRIES),
    )

    print("Notion client initialized.")

    return notion_client, notion_databases
