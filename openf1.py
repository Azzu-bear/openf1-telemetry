"""Thin, readable client for the OpenF1 API (https://openf1.org).

Why this file exists
--------------------
OpenF1 is a plain REST API: every endpoint returns a JSON list of flat records.
There is no SDK to hide things from you, which makes it a good API to learn on.
The only two things worth wrapping are:

1. Query operators. OpenF1 filters with comparison operators embedded in the
   parameter *name*, e.g. `?date>2026-11-08T17:31:10`. `requests` would
   percent-encode that `>` into the parameter name in a way the server does not
   parse, so we build the query string by hand.
2. Caching. Session data never changes once a session is over, and car telemetry
   responses are large. We cache every response on disk so you can iterate on
   plots without hammering the API.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import quote

import requests

BASE_URL = "https://api.openf1.org/v1"
CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

# OpenF1 is free and unauthenticated but rate limited. Be polite.
_MIN_SECONDS_BETWEEN_CALLS = 0.25
_last_call_at = 0.0


def _build_url(endpoint: str, params: dict) -> str:
    """Build an OpenF1 URL, keeping comparison operators intact.

    `params` keys may carry an operator suffix, e.g. {"date>": "...", "date<": "..."}.
    We percent-encode the key but leave `<` and `>` alone, because OpenF1 splits
    name from value on the first `=`, so `date%3E=2026-...` arrives as
    name `date>`, value `2026-...`.
    """
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        safe_key = quote(str(key), safe="<>")
        safe_value = quote(str(value), safe=":-.")
        pairs.append(f"{safe_key}={safe_value}")
    query = "&".join(pairs)
    return f"{BASE_URL}/{endpoint}?{query}" if query else f"{BASE_URL}/{endpoint}"


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{digest}.json"


def fetch_json(url: str, use_cache: bool = True, headers: dict | None = None, empty=None):
    """GET one URL and return parsed JSON, cached on disk and rate limited.

    `empty` is what to return on a 404, which both APIs here use to mean "no
    records matched" rather than "something went wrong".
    """
    global _last_call_at

    cached = _cache_path(url)
    if use_cache and cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    elapsed = time.monotonic() - _last_call_at
    if elapsed < _MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(_MIN_SECONDS_BETWEEN_CALLS - elapsed)

    response = requests.get(url, timeout=60, headers=headers)
    _last_call_at = time.monotonic()

    if response.status_code == 404:
        if use_cache:
            cached.write_text(json.dumps(empty), encoding="utf-8")
        return empty

    response.raise_for_status()
    payload = response.json()

    if use_cache:
        cached.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def get(endpoint: str, use_cache: bool = True, **params) -> list[dict]:
    """GET one OpenF1 endpoint and return its records.

    OpenF1 answers 404 when a query matches no records, which happens all the
    time for sessions that have not run yet. That is an empty result, not an
    error, so callers get a plain empty list.

    >>> get("sessions", year=2026, session_type="Race")[0]["location"]
    'Melbourne'
    """
    url = _build_url(endpoint, params)
    records = fetch_json(url, use_cache=use_cache, empty=[])

    if not isinstance(records, list):
        raise RuntimeError(f"Unexpected OpenF1 response for {url}: {records!r}")
    return records


# --- Convenience wrappers for the endpoints this app uses -------------------


def meetings(year: int) -> list[dict]:
    """Race weekends in a season. A 'meeting' is the whole weekend."""
    return get("meetings", year=year)


def sessions(meeting_key: int) -> list[dict]:
    """Sessions within one weekend: FP1/FP2/FP3, Qualifying, Sprint, Race."""
    return get("sessions", meeting_key=meeting_key)


def drivers(session_key: int) -> list[dict]:
    """Entry list for a session, including team name and team colour."""
    return get("drivers", session_key=session_key)


def laps(session_key: int, driver_number: int | None = None) -> list[dict]:
    """Every timed lap, with sector times and the mini-sector segment codes."""
    return get("laps", session_key=session_key, driver_number=driver_number)


def stints(session_key: int) -> list[dict]:
    """Tyre stints: compound, age at start, and the lap range each covers."""
    return get("stints", session_key=session_key)


def pit(session_key: int) -> list[dict]:
    """Pit stops, with stationary duration in the pit box."""
    return get("pit", session_key=session_key)


def circuit_corners(circuit_key: int, year: int) -> list[dict]:
    """Numbered corners for a circuit, from the MultiViewer circuits API.

    This is NOT an OpenF1 endpoint. OpenF1 publishes no corner data, but every
    `/meetings` record carries a `circuit_info_url` pointing at MultiViewer,
    which is the same source FastF1 uses for `get_circuit_info()`.

    The coordinates share OpenF1's frame exactly, so no transform is needed:
    at Las Vegas the 17 published corners land a median of 1.3 m from a real
    car's measured racing line. Returns records with `number` and a
    `trackPosition` of x/y in tenths of a metre.
    """
    payload = fetch_json(
        f"https://api.multiviewer.app/api/v1/circuits/{circuit_key}/{year}",
        headers={"User-Agent": "openf1-telemetry-dashboard/1.0"},
        empty={},
    )
    return (payload or {}).get("corners", [])


def location(session_key: int, driver_number: int, start_iso: str, end_iso: str) -> list[dict]:
    """~3.7 Hz track position as x, y, z.

    Coordinates arrive in tenths of a metre, in the circuit's own local frame
    (the origin is arbitrary and differs per track), so they are only ever
    meaningful relative to each other.
    """
    return get(
        "location",
        session_key=session_key,
        driver_number=driver_number,
        **{"date>": start_iso, "date<": end_iso},
    )


def car_data(session_key: int, driver_number: int, start_iso: str, end_iso: str) -> list[dict]:
    """~3.7 Hz car telemetry: speed, throttle, brake, n_gear, rpm, drs.

    `start_iso` / `end_iso` are naive ISO timestamps (no timezone suffix), which
    is the format OpenF1's date filters expect.
    """
    return get(
        "car_data",
        session_key=session_key,
        driver_number=driver_number,
        **{"date>": start_iso, "date<": end_iso},
    )
