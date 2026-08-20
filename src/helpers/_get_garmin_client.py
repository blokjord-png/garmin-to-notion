"""Garmin Connect client construction with a persisted, self-refreshing session.

The previous version passed GARMIN_AUTH_TOKEN straight to garminconnect as an
in-memory token string (`tokenstore=<json>`). garminconnect refreshes the
access token on every run via diauth.garmin.com, but that refreshed token was
never written back anywhere - so once Garmin invalidated or rotated the
underlying refresh token, every run kept retrying with the same stale token
from the GARMIN_AUTH_TOKEN secret and failed with a 401 forever.

This version keeps the session in a directory (GARMIN_TOKENSTORE_DIR, default
".garmin_tokenstore") that the GitHub Actions workflow caches between runs.
garminconnect >=0.3.6 refreshes tokens proactively when loading from a
directory and persists the refreshed tokens back to that same directory. If
GARMIN_EMAIL/GARMIN_PASSWORD secrets are also set, it additionally falls back
to a fresh credential login when the cached session is rejected outright
(e.g. Garmin invalidated it), again writing the renewed session back to the
directory. That's what gets cached for the next run, closing the loop that
was missing before.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin


@dataclass(frozen=True)
class GarminConfiguration:
    activity_fetch_limit: int


def get_garmin_client() -> tuple[Garmin, GarminConfiguration]:
    load_dotenv()

    print("Initializing Garmin client...")

    garmin_client = _get_garmin_client()
    garmin_configuration = _get_garmin_configuration()

    print("Garmin client authenticated successfully.")

    return garmin_client, garmin_configuration


def _tokenstore_dir() -> Path:
    path = Path(os.getenv("GARMIN_TOKENSTORE_DIR", ".garmin_tokenstore"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _token_file(tokenstore_dir: Path) -> Path:
    return tokenstore_dir / "garmin_tokens.json"


def _seed_tokenstore_from_secret(tokenstore_dir: Path) -> None:
    """First run ever (or a cache miss after eviction): seed the tokenstore
    directory from the GARMIN_AUTH_TOKEN secret, so a fresh checkout still
    works without requiring GARMIN_EMAIL/GARMIN_PASSWORD to be set. Once
    garminconnect refreshes the session, it overwrites this same file with
    the renewed one, and the workflow caches that for next time."""
    if _token_file(tokenstore_dir).exists():
        return

    garmin_auth_token = os.getenv("GARMIN_AUTH_TOKEN")
    if not garmin_auth_token:
        return

    try:
        data = json.loads(garmin_auth_token)
    except ValueError as e:
        raise ValueError(
            "GARMIN_AUTH_TOKEN is not valid JSON. See README_AUTH_SETUP.md "
            "for instructions on generating a fresh token."
        ) from e

    token_file = _token_file(tokenstore_dir)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _get_garmin_client() -> Garmin:
    tokenstore_dir = _tokenstore_dir()
    _seed_tokenstore_from_secret(tokenstore_dir)

    garmin_email = os.getenv("GARMIN_EMAIL") or None
    garmin_password = os.getenv("GARMIN_PASSWORD") or None

    if not _token_file(tokenstore_dir).exists() and not (
        garmin_email and garmin_password
    ):
        raise ValueError(
            "No cached Garmin session and no GARMIN_AUTH_TOKEN or "
            "GARMIN_EMAIL+GARMIN_PASSWORD secret available. See "
            "README_AUTH_SETUP.md for instructions on generating a token."
        )

    # Passing email/password (when available) lets garminconnect fall back to
    # a fresh login automatically if the cached session is ever rejected
    # outright, instead of just raising a 401 like before.
    garmin_client = Garmin(email=garmin_email, password=garmin_password)
    garmin_client.login(tokenstore=str(tokenstore_dir))

    return garmin_client


def _get_garmin_configuration() -> GarminConfiguration:
    return GarminConfiguration(
        activity_fetch_limit=int(os.getenv("GARMIN_ACTIVITIES_FETCH_LIMIT", "10")),
    )
