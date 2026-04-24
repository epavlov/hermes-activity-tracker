"""Shared calendar / work-schedule helpers for activity-tracker.

Loads:
    data/holidays.json          — company holidays
    data/work_schedule.json     — weekly WFH / office / off pattern

Exposes lookups used by analyze.py and notify.py:
    is_holiday(d), is_weekend(d), is_day_off(d)
    holiday_name(d) -> str | None
    work_mode(d)   -> "off" | "wfh" | "office"
    consecutive_off_starting(d, max_len=14) -> int
    off_block_containing(d) -> (start_date, end_date, length) | None
"""

from __future__ import annotations

import datetime as dt
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _SKILL_ROOT / "data"


@lru_cache(maxsize=1)
def _load_holidays() -> dict[str, str]:
    path = _DATA_DIR / "holidays.json"
    if not path.exists():
        return {}
    return {row["date"]: row["name"] for row in json.loads(path.read_text())}


@lru_cache(maxsize=1)
def _load_work_schedule() -> dict[str, Any]:
    path = _DATA_DIR / "work_schedule.json"
    if not path.exists():
        return {"hours": {"start": "09:00", "end": "16:00"}, "weekly": {}}
    return json.loads(path.read_text())


_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_holiday(day: dt.date) -> bool:
    return day.isoformat() in _load_holidays()


def holiday_name(day: dt.date) -> str | None:
    return _load_holidays().get(day.isoformat())


def is_weekend(day: dt.date) -> bool:
    return day.weekday() >= 5


def is_day_off(day: dt.date) -> bool:
    return is_weekend(day) or is_holiday(day)


def work_mode(day: dt.date) -> str:
    """Return "off", "wfh", or "office" for a given date."""
    if is_day_off(day):
        return "off"
    key = _DAY_KEYS[day.weekday()]
    weekly = _load_work_schedule().get("weekly", {})
    mode = weekly.get(key, "off")
    return mode if mode in {"off", "wfh", "office"} else "off"


def work_hours() -> tuple[str, str]:
    hours = _load_work_schedule().get("hours", {})
    return hours.get("start", "09:00"), hours.get("end", "16:00")


def consecutive_off_starting(day: dt.date, max_len: int = 14) -> int:
    """Count off-days starting at `day` and walking forward. Returns 0 if `day` is a work day."""
    count = 0
    d = day
    while count < max_len and is_day_off(d):
        count += 1
        d += dt.timedelta(days=1)
    return count


def off_block_containing(day: dt.date, max_radius: int = 14) -> tuple[dt.date, dt.date, int] | None:
    """If `day` sits inside a run of off-days, return (start, end, length). Else None."""
    if not is_day_off(day):
        return None
    start = day
    while (start - dt.timedelta(days=1)).year >= 1 and is_day_off(start - dt.timedelta(days=1)):
        start -= dt.timedelta(days=1)
        if (day - start).days > max_radius:
            break
    end = day
    while is_day_off(end + dt.timedelta(days=1)):
        end += dt.timedelta(days=1)
        if (end - day).days > max_radius:
            break
    return start, end, (end - start).days + 1


def long_weekend_adjacent_holiday(day: dt.date) -> str | None:
    """If `day` is inside a 3+ day off-block that contains a holiday, return the holiday name."""
    block = off_block_containing(day)
    if not block or block[2] < 3:
        return None
    start, end, _ = block
    d = start
    while d <= end:
        name = holiday_name(d)
        if name:
            return name
        d += dt.timedelta(days=1)
    return None
