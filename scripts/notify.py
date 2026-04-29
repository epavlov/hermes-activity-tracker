"""Format and send Telegram suggestions for activity-tracker.

Reads ~/activity-ideas.json (must have been written by analyze.py first),
builds a Markdown V2 message, and POSTs to Telegram. Requires:

    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=12345678

Usage:
    python3 notify.py            # build + send
    python3 notify.py --dry-run  # print the message, don't send
    python3 notify.py --test     # send a "hello" message and exit
    python3 notify.py --json F   # use a fixture JSON
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from weather import get_forecast, summary_line
    from schedule import (
        holiday_name,
        long_weekend_adjacent_holiday,
        off_block_containing,
        work_mode,
    )
    from analyze import activity_span_days
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from weather import get_forecast, summary_line  # type: ignore
    from schedule import (  # type: ignore
        holiday_name,
        long_weekend_adjacent_holiday,
        off_block_containing,
        work_mode,
    )
    from analyze import activity_span_days  # type: ignore


DEFAULT_JSON_PATH = Path(os.path.expanduser("~/activity-ideas.json"))
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Characters that must be escaped in Telegram MarkdownV2.
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def mdv2_escape(text: str) -> str:
    """Escape MarkdownV2 specials. Apply to plain text only, not markup."""
    return "".join("\\" + c if c in _MDV2_SPECIALS else c for c in text)


def _fmt_date(iso_or_date: str | dt.date) -> str:
    d = iso_or_date if isinstance(iso_or_date, dt.date) else dt.date.fromisoformat(iso_or_date)
    return d.strftime("%a, %b %-d") if sys.platform != "win32" else d.strftime("%a, %b %#d")


def _first_suggested(entry: dict[str, Any]) -> str | None:
    dates = entry.get("suggested_dates") or []
    return dates[0] if dates else None


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text() or "[]")


def _date_range_label(start_iso: str, span: int) -> tuple[str, str | None]:
    """
    Return (date_label, holiday_note).

    For multi-day activities, format "Sat, May 23 → Mon, May 25". Otherwise
    single-day label. If the range intersects a company holiday, include
    its name as the second element.
    """
    try:
        start = dt.date.fromisoformat(start_iso)
    except ValueError:
        return start_iso, None

    if span < 2:
        return _fmt_date(start), holiday_name(start)

    end = start + dt.timedelta(days=span - 1)
    label = f"{_fmt_date(start)} → {_fmt_date(end)}"

    # Any holiday inside the span?
    holiday_note = None
    d = start
    while d <= end:
        name = holiday_name(d)
        if name:
            holiday_note = name
            break
        d += dt.timedelta(days=1)

    if not holiday_note:
        # Adjacent holiday (e.g. Sun start where Mon is a holiday and part of the off-block)
        holiday_note = long_weekend_adjacent_holiday(start)

    return label, holiday_note


def build_message(entries: list[dt.date | dict[str, Any]], today: dt.date | None = None) -> str:
    today = today or dt.date.today()

    scheduled_future = []
    for e in entries:
        if e.get("status") != "scheduled":
            continue
        iso = _first_suggested(e)
        if not iso:
            continue
        try:
            d = dt.date.fromisoformat(iso)
        except ValueError:
            continue
        # Multi-day activities should still show until the range has fully passed.
        span = activity_span_days(e)
        last_day = d + dt.timedelta(days=max(span - 1, 0))
        if last_day >= today:
            scheduled_future.append((d, e))
    scheduled_future.sort(key=lambda x: x[0])

    pending = [e for e in entries if e.get("status") in ("pending", "unassigned")]

    lines: list[str] = []
    lines.append("📅 *Activity Date Suggestions*")
    lines.append("")

    if scheduled_future:
        lines.append("✅ *Upcoming — already scheduled:*")
        for d, e in scheduled_future:
            span = activity_span_days(e)
            label, holiday = _date_range_label(d.isoformat(), span)
            name = mdv2_escape(e["name"])
            label_md = mdv2_escape(label)
            suffix = f" \\({mdv2_escape(holiday)}\\)" if holiday else ""
            lines.append(f"• {name} — {label_md}{suffix}")
        lines.append("")
        if pending:
            # Visible divider between "already scheduled" and "suggestions".
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append("")

    if pending:
        lines.append("📝 *Suggestions — reply with the number to accept:*")
        lines.append("")

        weather_cache: dict[str, dict[str, dict[str, Any]]] = {}

        for idx, e in enumerate(pending, 1):
            name = mdv2_escape(e["name"])
            iso = _first_suggested(e)
            span = activity_span_days(e)

            if iso:
                label, holiday = _date_range_label(iso, span)
            else:
                label, holiday = "(no date)", None
            label_md = mdv2_escape(label)
            holiday_md = f" \\({mdv2_escape(holiday)}\\)" if holiday else ""
            lines.append(f"{idx}\\. *{name}* — {label_md}{holiday_md}")

            bits = [
                e.get("category", ""),
                e.get("indoor_or_outdoor", ""),
                e.get("duration", ""),
                e.get("best_time", ""),
            ]
            meta = mdv2_escape(" · ".join(b for b in bits if b))

            if e.get("weather_dependent") and e.get("location") and iso:
                loc = e["location"]
                if loc not in weather_cache:
                    weather_cache[loc] = get_forecast(loc)
                fc_day = weather_cache[loc].get(iso)
                if fc_day:
                    meta += mdv2_escape(" · " + summary_line(fc_day))

            lines.append(f"   {meta}")
            lines.append("")

    lines.append(mdv2_escape("Reply: number to accept · date to override · all · skip · history"))
    return "\n".join(lines)


def _load_env_token(env_path: Path) -> str | None:
    """Load TELEGRAM_BOT_TOKEN from the Hermes .env file as fallback."""
    if not env_path.exists():
        return None
    text = env_path.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("TELEGRAM_BOT_TOKEN="):
            val = stripped.split("=", 1)[1].strip()
            if val and val != "***":
                return val
    return None


def _load_env_chat_id(env_path: Path) -> str | None:
    """Load TELEGRAM_HOME_CHANNEL from the Hermes .env file as fallback."""
    if not env_path.exists():
        return None
    text = env_path.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("TELEGRAM_HOME_CHANNEL="):
            val = stripped.split("=", 1)[1].strip()
            if val and val != "***":
                return val
    return None


def send(message: str, token: str | None = None, chat_id: str | None = None) -> dict[str, Any]:
    env_path = Path(os.path.expanduser("~/.hermes/.env"))
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN") or _load_env_token(env_path)
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_HOME_CHANNEL") or _load_env_chat_id(env_path)
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or TELEGRAM_HOME_CHANNEL as fallback)")

    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    req = urllib.request.Request(TELEGRAM_API.format(token=token), data=payload)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test", action="store_true", help="Send a hello message and exit.")
    args = parser.parse_args()

    if args.test:
        msg = mdv2_escape("activity-tracker: hello from notify.py")
        if args.dry_run:
            print(msg)
            return 0
        result = send(msg)
        print(json.dumps(result))
        return 0 if result.get("ok") else 1

    entries = _load(args.json)
    message = build_message(entries)

    if args.dry_run:
        print(message)
        return 0

    if not entries:
        print("No entries to notify about, skipping.")
        return 0

    result = send(message)
    if not result.get("ok"):
        print(json.dumps(result), file=sys.stderr)
        return 1
    print("Sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
