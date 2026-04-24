"""Deterministic scorer for activity-tracker.

Reads ~/activity-ideas.json, scores candidate dates for each pending /
unassigned activity, and writes the top ISO dates back into
`suggested_dates`. The scoring is deterministic given the same input JSON,
same weather cache, and same holidays/work_schedule data files.

Scoring components
------------------
1. Day-of-week fit (category-dependent).
2. Weekend/holiday bump — bigger than before, since the user wants more
   weekend activities.
3. Long-weekend block fit — for multi-day / travel activities, check the
   full span: "perfect" if every day in the span is off, partial credit if
   one workday bleeds in (e.g. leave after work Friday).
4. Work-schedule conflict — penalizes weekday time-of-day slots that
   collide with 9–4 work. Tue/Wed (office) get a stiffer penalty than
   Mon/Thu/Fri (WFH) because of commute overhead.
5. Weather fit — unchanged (only for `weather_dependent` activities).
6. Preferred-date bonus — user's natural-language ranges.
7. Urgency decay — minor bias toward the near term so the backlog moves.

Activity span parsing
---------------------
Span is the number of consecutive days an activity occupies. Controls both
the scoring window (long-weekend check) and the candidate horizon.
    span = 0   → single slot inside a day  (e.g. "60-90 min")
    span = 1   → one full day              (e.g. "full day", "all day")
    span = N   → multi-day                 ("2 days", "weekend", "trip", travel category)

Usage:
    python3 analyze.py              # score and write in place
    python3 analyze.py --dry-run    # print scores, do not write
    python3 analyze.py --migrate    # fill missing fields + migrate legacy fields, exit
    python3 analyze.py --json F     # use a fixture instead of ~/activity-ideas.json
    python3 analyze.py --today ISO  # override "today" (useful for testing)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from weather import get_forecast
    from schedule import (
        consecutive_off_starting,
        holiday_name,
        is_day_off,
        is_holiday,
        is_weekend,
        long_weekend_adjacent_holiday,
        off_block_containing,
        work_mode,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from weather import get_forecast  # type: ignore
    from schedule import (  # type: ignore
        consecutive_off_starting,
        holiday_name,
        is_day_off,
        is_holiday,
        is_weekend,
        long_weekend_adjacent_holiday,
        off_block_containing,
        work_mode,
    )


DEFAULT_JSON_PATH = Path(os.path.expanduser("~/activity-ideas.json"))

ACTIVE_STATUSES = {"pending", "unassigned"}
TOP_N = 3

# Span-dependent horizons (days out from today).
HORIZON_BY_SPAN = {
    0: 14,   # short slot activities
    1: 21,   # full-day
}
MULTI_DAY_HORIZON = 60  # span >= 2 (travel etc.)

# How close suggested dates can be to each other. Multi-day activities need
# more breathing room so consecutive weekend suggestions don't overlap.
MIN_GAP_SHORT = 2
MIN_GAP_MULTI = 7


# Day-of-week preference per category.
# (weekend_points, weekday_points)
DOW_PREF: dict[str, tuple[int, int]] = {
    "work":     (-5, 15),
    "learning": (0,  15),
    "fitness":  (10, 10),
    "health":   (5,  5),
    "personal": (10, 5),
    "creative": (15, 5),
    "outdoor":  (20, 5),
    "indoor":   (5,  5),
    "social":   (25, 0),
    "travel":   (25, -5),
}

DEFAULTS = {
    "category": "personal",
    "status": "pending",
    "location": None,
    "indoor_or_outdoor": "indoor",
    "weather_dependent": False,
    "duration": "unspecified",
    "best_time": "flexible",
    "preferred_dates": None,
    "suggested_dates": [],
    "notes": None,
}


# ---------- span parsing ----------

_DAY_RE = re.compile(r"(\d+)\s*(?:day|days|d)\b", re.IGNORECASE)
_HOUR_RE = re.compile(r"(\d+)\s*(?:hour|hours|h|hr|hrs)\b", re.IGNORECASE)


def activity_span_days(entry: dict[str, Any]) -> int:
    """Return how many consecutive days the activity occupies (0 = sub-day slot)."""
    duration = (entry.get("duration") or "").lower()
    if m := _DAY_RE.search(duration):
        return max(int(m.group(1)), 1)
    if "weekend" in duration:
        return 2
    if "trip" in duration:
        return 3
    if "full day" in duration or "all day" in duration:
        return 1
    if entry.get("category") == "travel":
        return 3  # sensible default if duration wasn't specified
    return 0


# ---------- defaults / migration ----------

def _fill_defaults(entry: dict[str, Any]) -> dict[str, Any]:
    """Fill missing fields. Migrate legacy `type` → `indoor_or_outdoor`."""
    if "type" in entry and "indoor_or_outdoor" not in entry:
        legacy = entry.pop("type")
        if legacy in {"indoor", "outdoor", "mixed"}:
            entry["indoor_or_outdoor"] = legacy
    for key, default in DEFAULTS.items():
        entry.setdefault(key, default)
    return entry


# ---------- scoring helpers ----------

def _preferred_date_bonus(preferred: list[str] | None, candidate: dt.date) -> int:
    if not preferred:
        return 0
    candidate_month = candidate.strftime("%B").lower()
    candidate_half_early = candidate.day <= 15
    candidate_end = candidate.day >= 23
    for phrase in preferred:
        p = phrase.lower()
        if candidate_month in p:
            if "early" in p and candidate_half_early:
                return 15
            if ("late" in p or "end of" in p) and candidate_end:
                return 15
            return 10
    return 0


def _long_weekend_score(entry: dict[str, Any], day: dt.date, span: int) -> int:
    """
    Score a candidate START date for a multi-day activity.

    Perfect: all `span` days are off (e.g. Sat–Mon with Monday holiday).
    Good:    span-1 off + 1 workday (e.g. Fri-work-return-after Sun — rare; or leave-after-work Fri).
    Bad:     less than span-1 off.

    Extra bonus if the off-block contains an official holiday, since those
    are the "long weekends" the user specifically called out for travel.
    """
    if span < 2:
        return 0

    off_count = sum(is_day_off(day + dt.timedelta(days=i)) for i in range(span))

    if off_count == span:
        base = 40
    elif off_count == span - 1:
        # One workday bleeding in. Better if that workday is the *last* one
        # (leave Friday evening → return Monday) or the *first* (Sat-Sun-Mon
        # with Monday as office). Acceptable either way.
        base = 15
    else:
        base = -15

    # Holiday-adjacent bonus: if any day in the block is a company holiday.
    if any(is_holiday(day + dt.timedelta(days=i)) for i in range(span)):
        base += 10
    else:
        adjacent = long_weekend_adjacent_holiday(day)
        if adjacent:
            base += 5

    # Prefer Friday/Saturday starts for travel — they feel natural.
    if day.weekday() in (4, 5):  # Fri, Sat
        base += 5

    return base


def _work_conflict_score(entry: dict[str, Any], day: dt.date) -> int:
    """
    Penalty for weekday activities that collide with 9-4 work.

    Office days (Tue/Wed) are stricter: commute means less lunch-break
    flexibility and no "pop out mid-morning" option.
    """
    mode = work_mode(day)
    if mode == "off":
        return 0

    best_time = entry.get("best_time", "flexible")
    span = activity_span_days(entry)

    # Multi-day activities on a weekday are already handled by long-weekend scoring.
    if span >= 1:
        return -10 if mode == "office" else -5

    if best_time == "morning":
        return -10 if mode == "office" else -2
    if best_time == "evening":
        return 0  # work ends at 4
    if best_time in {"afternoon", "daytime"}:
        return -20 if mode == "office" else -12
    # "flexible"
    return -8 if mode == "office" else -4


def _dow_score(entry: dict[str, Any], day: dt.date) -> int:
    category = entry.get("category", "personal")
    weekend_pts, weekday_pts = DOW_PREF.get(category, (5, 5))
    return weekend_pts if is_weekend(day) else weekday_pts


def _holiday_bonus(entry: dict[str, Any], day: dt.date) -> int:
    """Holidays act like weekends but with an extra bump."""
    if not is_holiday(day):
        return 0
    category = entry.get("category", "personal")
    weekend_pts, _ = DOW_PREF.get(category, (5, 5))
    return max(5, weekend_pts // 2)


def _weather_score(entry: dict[str, Any], day: dt.date, forecast: dict[str, dict[str, Any]]) -> int:
    if not entry.get("weather_dependent"):
        return 0
    iso = day.isoformat()
    fc = forecast.get(iso)
    if fc is None:
        return 0
    score = 0
    rain = fc.get("max_rain_chance", 0)
    cloud = fc.get("avg_cloud", 0)
    if rain > 50:
        score -= 30
    elif rain < 20:
        score += 15
    if entry.get("indoor_or_outdoor") == "outdoor" and cloud > 80:
        score -= 10
    return score


def _urgency_score(day: dt.date, today: dt.date) -> int:
    days_out = (day - today).days
    return max(0, 5 - days_out // 5)


def _score_day(
    entry: dict[str, Any],
    day: dt.date,
    forecast: dict[str, dict[str, Any]],
    today: dt.date,
) -> tuple[int, dict[str, int]]:
    span = activity_span_days(entry)
    breakdown: dict[str, int] = {}
    breakdown["dow"] = _dow_score(entry, day)
    breakdown["holiday"] = _holiday_bonus(entry, day)
    breakdown["long_weekend"] = _long_weekend_score(entry, day, span)
    breakdown["work_conflict"] = _work_conflict_score(entry, day)
    breakdown["weather"] = _weather_score(entry, day, forecast)
    breakdown["preferred"] = _preferred_date_bonus(entry.get("preferred_dates"), day)
    breakdown["urgency"] = _urgency_score(day, today)
    return sum(breakdown.values()), breakdown


# ---------- picking ----------

def _pick_top_with_spread(
    scored: list[tuple[dt.date, int]], top_n: int, min_gap_days: int
) -> list[dt.date]:
    scored_sorted = sorted(scored, key=lambda x: (-x[1], x[0]))
    picked: list[dt.date] = []
    for day, _score in scored_sorted:
        if all(abs((day - p).days) >= min_gap_days for p in picked):
            picked.append(day)
        if len(picked) == top_n:
            break
    if len(picked) < top_n:
        for day, _score in scored_sorted:
            if day not in picked:
                picked.append(day)
            if len(picked) == top_n:
                break
    return picked


# ---------- top-level ----------

def analyze(entries: list[dict[str, Any]], today: dt.date | None = None) -> list[dict[str, Any]]:
    today = today or dt.date.today()
    forecast_cache: dict[str, dict[str, dict[str, Any]]] = {}

    for entry in entries:
        _fill_defaults(entry)
        if entry["status"] not in ACTIVE_STATUSES:
            continue

        span = activity_span_days(entry)
        horizon = HORIZON_BY_SPAN.get(span, MULTI_DAY_HORIZON)
        candidate_days = [today + dt.timedelta(days=i) for i in range(1, horizon + 1)]

        location = entry.get("location")
        forecast: dict[str, dict[str, Any]] = {}
        if entry.get("weather_dependent") and location:
            if location not in forecast_cache:
                forecast_cache[location] = get_forecast(location)
            forecast = forecast_cache[location]

        scored = [(day, _score_day(entry, day, forecast, today)[0]) for day in candidate_days]
        min_gap = MIN_GAP_MULTI if span >= 2 else MIN_GAP_SHORT
        top_dates = _pick_top_with_spread(scored, TOP_N, min_gap)
        entry["suggested_dates"] = [d.isoformat() for d in top_dates]
    return entries


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text() or "[]")


def _save(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--migrate", action="store_true", help="Fill defaults, migrate legacy fields, exit.")
    parser.add_argument("--today", type=str, help="Override today (ISO date) for testing.")
    parser.add_argument("--explain", type=str, help="Entry id to print full score breakdown for.")
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    entries = _load(args.json)

    if args.migrate:
        for e in entries:
            _fill_defaults(e)
        _save(args.json, entries)
        print(f"Migrated {len(entries)} entries in {args.json}")
        return 0

    if args.explain:
        target = next((e for e in entries if e.get("id") == args.explain), None)
        if not target:
            print(f"No entry with id={args.explain!r}", file=sys.stderr)
            return 1
        _fill_defaults(target)
        span = activity_span_days(target)
        horizon = HORIZON_BY_SPAN.get(span, MULTI_DAY_HORIZON)
        forecast = get_forecast(target["location"]) if target.get("weather_dependent") and target.get("location") else {}
        print(f"{target['id']} — {target['name']} (span={span}, horizon={horizon} days)")
        print(f"{'date':12} {'total':>5}  breakdown")
        for i in range(1, horizon + 1):
            d = today + dt.timedelta(days=i)
            total, bd = _score_day(target, d, forecast, today)
            mode = work_mode(d)
            tag = "off " if mode == "off" else mode
            print(f"{d.isoformat()}  {d.strftime('%a')}  {tag}  {total:+4d}  {bd}")
        return 0

    entries = analyze(entries, today=today)

    if args.dry_run:
        for e in entries:
            if e["status"] in ACTIVE_STATUSES:
                span = activity_span_days(e)
                tag = f"span={span}"
                print(f"{e['id']:18} {e['name'][:38]:38} {tag:8}  {e['suggested_dates']}")
        return 0

    _save(args.json, entries)
    n_active = sum(1 for e in entries if e["status"] in ACTIVE_STATUSES)
    print(f"Scored {n_active} active entries, wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
