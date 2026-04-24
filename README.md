# hermes-activity-tracker

A [Hermes Agent](https://github.com/) skill that turns a backlog of "things I want to do someday" into actionable date suggestions delivered to Telegram twice a day.

Instead of letting ideas rot in a notes app, you capture them in plain chat with the agent — _"I want to go hiking in the Catskills"_, _"Nashville trip someday"_, _"learn Spanish"_ — and a deterministic scorer picks concrete dates for each one, weighted by category, weather, your work schedule, and company holidays.

## Features

- **Deterministic scoring.** Same input → same output. The model captures entries; the scorer runs in plain Python with no LLM in the loop.
- **Long-weekend awareness.** Multi-day activities (travel, weekend trips, 2+ day hikes) automatically land on holiday-adjacent blocks — Memorial Day, Juneteenth, Labor Day, etc.
- **Work-schedule conflict detection.** Knows which days you're WFH vs in-office and penalizes weekday slot-fit accordingly (office days are stricter because of commute overhead).
- **Weather integration** for outdoor, location-bound activities via [wttr.in](https://wttr.in), with file-based caching and graceful degradation.
- **Telegram delivery** at 07:00 and 19:00 via cron. Numbered replies (`1`, `2`) or free-form dates accepted in chat.

## Quick start

```bash
git clone https://github.com/<your-username>/hermes-activity-tracker.git \
    ~/.hermes/skills/productivity/activity-tracker
cd ~/.hermes/skills/productivity/activity-tracker

# initialize the backlog
echo '[]' > ~/activity-ideas.json

# Telegram credentials (see Setup section in SKILL.md)
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="12345678"

# smoke-test delivery
python3 scripts/notify.py --test

# register the cron
( crontab -l 2>/dev/null; echo '0 7,19 * * * cd ~/.hermes/skills/productivity/activity-tracker && python3 scripts/analyze.py && python3 scripts/notify.py' ) | crontab -
```

Customize `data/work_schedule.json` for your own WFH/office pattern, and refresh `data/holidays.json` each year.

## Repository layout

```
hermes-activity-tracker/
├── SKILL.md                 # agent-facing skill instructions
├── README.md                # this file
├── data/
│   ├── holidays.json        # company holidays (update yearly)
│   └── work_schedule.json   # weekly WFH / office / off pattern
└── scripts/
    ├── analyze.py           # scoring + JSON mutation
    ├── schedule.py          # holiday + work-schedule helpers
    ├── weather.py           # wttr.in wrapper with caching
    └── notify.py            # Telegram formatter + sender
```

## How scoring works

Each pending activity is assigned a span (sub-day / full-day / multi-day), which drives the candidate horizon (14 / 21 / 60 days) and whether long-weekend block detection applies. Each candidate day is scored on:

| component | purpose |
| --- | --- |
| day-of-week fit | social/travel/outdoor prefer weekends; work/learning prefer weekdays |
| holiday bonus | extra bump for company holidays |
| long-weekend block (span ≥ 2) | checks if the full span is off; big bonus if it contains a holiday |
| work-schedule conflict | penalizes weekday activities that collide with 9–4 work (office days harsher) |
| weather fit | only for weather-dependent outdoor activities |
| preferred-date bonus | matches hints like `"early May"` |
| urgency decay | mild pull toward the near term |

Top 3 dates per activity are written back to the `suggested_dates` array. Multi-day picks enforce a 7-day spread between suggestions so you don't get three consecutive weekend proposals.

For the full weights and behavior, see [`SKILL.md`](SKILL.md) and [`scripts/analyze.py`](scripts/analyze.py). Use `python3 scripts/analyze.py --explain <id>` to inspect per-day breakdowns.

## Example Telegram output

```
📅 Activity Date Suggestions

🗓️ Scheduled next 14 days:
• Morning Yoga — Tue, May 26
• Nashville trip — Sat, May 23 → Mon, May 25 (Memorial Day)

📝 Pending — reply with the number to accept:

1. Learn Spanish vocabulary — Mon, Apr 27
   learning · indoor · 60-90 min · morning

2. Hiking at Zermatt — Sat, May 2
   outdoor · 2-3 hours · daytime · ☀️ rain 10% / cloud 30%

3. Cook Italian dinner — Sat, Apr 25
   social · indoor · evening

Reply: number to accept · date to override · all · skip · history
```

## License

MIT — see [LICENSE](LICENSE).
