"""wttr.in wrapper for activity-tracker.

Returns per-day forecast dicts keyed by ISO date:
    {"2026-04-24": {"max_rain_chance": 30, "avg_cloud": 55, "min_temp": 8, "max_temp": 17, "description": "Partly cloudy"}, ...}

Caches raw wttr.in responses at /tmp/weather_<slug>.json for 3 hours to stay
well under the (unpublished) rate limit. wttr.in occasionally returns strings
where you'd expect ints ("chanceofrain": "45"), so every numeric field is
coerced with _safe_int / _safe_float and a missing value falls back to 0.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CACHE_DIR = Path("/tmp")
CACHE_TTL_SECONDS = 3 * 60 * 60


def _slug(location: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", location.lower()).strip("_") or "unknown"


def _cache_path(location: str) -> Path:
    return CACHE_DIR / f"weather_{_slug(location)}.json"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_raw(location: str, timeout: int = 10) -> dict[str, Any] | None:
    """Fetch from wttr.in with a 3h file cache. Returns None on failure."""
    cache = _cache_path(location)
    if cache.exists() and (time.time() - cache.stat().st_mtime) < CACHE_TTL_SECONDS:
        try:
            return json.loads(cache.read_text())
        except json.JSONDecodeError:
            cache.unlink(missing_ok=True)

    url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        # Serve the stale cache if we have one — better than nothing.
        if cache.exists():
            try:
                return json.loads(cache.read_text())
            except json.JSONDecodeError:
                return None
        return None

    try:
        cache.write_text(json.dumps(data))
    except OSError:
        pass
    return data


def get_forecast(location: str) -> dict[str, dict[str, Any]]:
    """Return {iso_date: summary_dict}. Empty dict if the fetch failed."""
    raw = _fetch_raw(location)
    if not raw or "weather" not in raw:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for day in raw.get("weather", []):
        date = day.get("date")
        if not date:
            continue
        hourly = day.get("hourly", []) or []
        rain_chances = [_safe_int(h.get("chanceofrain")) for h in hourly]
        cloud_covers = [_safe_int(h.get("cloudcover")) for h in hourly]

        # `lang_ru` is unreliable; fall back to weatherDesc, then empty string.
        descs = hourly[len(hourly) // 2].get("weatherDesc", []) if hourly else []
        description = (descs[0].get("value") if descs else "") or ""

        out[date] = {
            "max_rain_chance": max(rain_chances) if rain_chances else 0,
            "avg_cloud": sum(cloud_covers) // len(cloud_covers) if cloud_covers else 0,
            "min_temp": _safe_int(day.get("mintempC")),
            "max_temp": _safe_int(day.get("maxtempC")),
            "description": description.strip(),
        }
    return out


def summary_line(forecast_day: dict[str, Any]) -> str:
    """Short one-line string suitable for Telegram output."""
    icon = "☀️"
    if forecast_day.get("max_rain_chance", 0) >= 50:
        icon = "🌧️"
    elif forecast_day.get("avg_cloud", 0) >= 70:
        icon = "☁️"
    return (
        f"{icon} rain {forecast_day.get('max_rain_chance', 0)}% / "
        f"cloud {forecast_day.get('avg_cloud', 0)}%"
    )


if __name__ == "__main__":
    import sys

    loc = sys.argv[1] if len(sys.argv) > 1 else "Zermatt"
    forecast = get_forecast(loc)
    if not forecast:
        print(f"No forecast available for {loc!r}")
        sys.exit(1)
    for date, info in sorted(forecast.items()):
        print(f"{date}  {summary_line(info)}  {info['min_temp']}–{info['max_temp']}°C  {info['description']}")
