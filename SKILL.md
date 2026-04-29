---
name: activity-tracker
description: Use this skill when the user (a) mentions an activity, hobby, outing, or idea they want to do at some point ("I'd like to learn Spanish", "we should go hiking sometime", "remind me to try that ramen place") — capture it into `~/activity-ideas.json`; or (b) asks what they should do today / this weekend / next, or replies to a Telegram activity suggestion — load the file, run the scoring scripts, and respond. Also invoked by the 7:00 and 19:00 cron jobs that re-score pending activities and push Telegram suggestions. Do NOT trigger for general calendar/scheduling tasks unrelated to the activity-ideas backlog.
---

# activity-tracker

Manages a personal backlog of activity ideas at `~/activity-ideas.json`, scores candidate dates for each pending idea, and sends Telegram suggestions twice a day. The LLM captures and edits entries; the bundled scripts do the deterministic scoring and delivery.

## When to use

- User describes something they want to do someday → append to the JSON as `pending`.
- User asks "what should I do [today/tomorrow/this weekend]?" → run `scripts/analyze.py`, read the result, answer.
- User replies to a Telegram suggestion (`1`, `2`, `all`, `skip`, `Tue May 26`, ...) → apply the reply to the JSON (see *Reply handling* below).
- Cron fires at 07:00 or 19:00 → run `scripts/analyze.py` then `scripts/notify.py`.

## Setup

Required once, before the cron can deliver:

1. Create a Telegram bot via @BotFather and note the token.
2. Send any message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID.
3. Export credentials so cron jobs inherit them (add to `~/.zshrc` or a launchd plist):
   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC..."
   export TELEGRAM_CHAT_ID="12345678"
   ```
4. Initialize the backlog if missing:
   ```bash
   [ -f ~/activity-ideas.json ] || echo '[]' > ~/activity-ideas.json
   ```
5. Register the cron (Hermes `cronjob` action, or plain crontab):
   ```
   0 7,19 * * * cd ~/.hermes/skills/productivity/activity-tracker && python3 scripts/analyze.py && python3 scripts/notify.py
   ```
6. Smoke test: `python3 scripts/notify.py --test` should deliver a "hello" message.

## Data model

Canonical shape for each entry in `~/activity-ideas.json` (array of objects):

| field | type | notes |
| --- | --- | --- |
| `id` | string | e.g. `act_001`, stable |
| `name` | string | user-facing title |
| `category` | enum | `personal`, `fitness`, `work`, `learning`, `social`, `creative`, `outdoor`, `indoor`, `travel`, `health` |
| `status` | enum | `pending`, `scheduled`, `unassigned`, `completed`, `abandoned`, `blocked` |
| `location` | string \| null | city or place name; required if `weather_dependent` is true |
| `indoor_or_outdoor` | enum | `indoor`, `outdoor`, `mixed` |
| `weather_dependent` | bool | if true, scorer fetches forecast for `location` |
| `duration` | string | e.g. `"60-90 min"`, `"2-3 hours"`, `"full day"` |
| `best_time` | enum | `morning`, `afternoon`, `evening`, `daytime`, `flexible` |
| `preferred_dates` | array\<string\> \| null | natural-language ranges (`"end of April"`, `"early May"`); advisory only |
| `suggested_dates` | array\<string\> | ISO dates written by `analyze.py`; top 3 candidates |
| `notes` | string \| null | free text |

There is no separate `type` field — `indoor_or_outdoor` is the single source of truth. If you encounter legacy entries with a `type` field, migrate on read.

Example entries:

```json
[
  {
    "id": "act_001",
    "name": "Learn Spanish vocabulary",
    "category": "learning",
    "status": "pending",
    "location": null,
    "indoor_or_outdoor": "indoor",
    "weather_dependent": false,
    "duration": "60-90 min",
    "best_time": "morning",
    "preferred_dates": null,
    "suggested_dates": [],
    "notes": "1-2 hours per session"
  },
  {
    "id": "act_002",
    "name": "Hiking at Mountain Trail",
    "category": "outdoor",
    "status": "pending",
    "location": "Zermatt",
    "indoor_or_outdoor": "outdoor",
    "weather_dependent": true,
    "duration": "2-3 hours",
    "best_time": "daytime",
    "preferred_dates": ["early May"],
    "suggested_dates": [],
    "notes": "Prefer sunny days"
  }
]
```

## Calendar data

Two static data files drive weekend / long-weekend / work-conflict logic. Both are loaded by `scripts/schedule.py`:

- `data/holidays.json` — array of `{date, name}` objects for 2026 company holidays. Update once a year.
- `data/work_schedule.json` — weekly pattern. Mon/Thu/Fri = `wfh`, Tue/Wed = `office`, Sat/Sun = `off`; default hours 09:00–16:00.

Office days matter for scoring: a weekday activity that collides with 9–4 takes a stiffer hit on Tue/Wed (commute + no pop-out flexibility) than on Mon/Thu/Fri (WFH).

## How scoring works

`scripts/analyze.py` iterates all entries with `status in {pending, unassigned}` and scores each candidate day within a span-dependent horizon. The top 3 dates per activity are written to `suggested_dates` (ISO `YYYY-MM-DD`).

### Activity span

Before scoring, each entry gets a span — how many consecutive days it occupies. Span drives the horizon and the long-weekend check:

| span | meaning | horizon |
| --- | --- | --- |
| 0 | sub-day slot (e.g. `"60-90 min"`, `"3 hours"`) | 14 days |
| 1 | full single day (`"full day"`, `"all day"`) | 21 days |
| ≥ 2 | multi-day (`"2 days"`, `"weekend"`, `"trip"`, or `category="travel"`) | 60 days |

Parsing rules (see `activity_span_days` in `analyze.py`):

- Explicit `"N day"` / `"N days"` → N
- `"weekend"` → 2
- `"trip"` → 3
- `"full day"` / `"all day"` → 1
- `category = "travel"` with no duration info → 3

### Score components

- **Day-of-week fit** — `social`, `travel`, `outdoor`, `creative` strongly prefer weekends; `work`, `learning`, `fitness` prefer weekdays; others mildly neutral. Weekend bonus is larger than before because the user wants more weekend activities.
- **Holiday bonus** — +5 to +12 on top of the weekend bump if the candidate day is a company holiday (see `data/holidays.json`).
- **Long-weekend block** (only for span ≥ 2) — scores the activity's START date against whether the next `span` days are all off. Perfect (all off) = +40. One workday bleeding in = +15. Less = −15. Extra +10 if the block contains a holiday, +5 if only adjacent. Fri/Sat starts get a small natural-feel bump. This is how Nashville-style travel lands on Memorial Day / Juneteenth / Labor Day weekends automatically.
- **Work-schedule conflict** (weekday non-holiday) — penalties scale with time-of-day and WFH vs office: office + daytime = −20, WFH + daytime = −12, evenings = 0 (work ends at 4), mornings mild (−10 office, −2 WFH). Applied to short/sub-day activities; multi-day activities are already handled by the long-weekend score.
- **Weather fit** (only if `weather_dependent=true`) — rain > 50% → −30, rain < 20% → +15, cloud > 80% on outdoor → −10.
- **Preferred-date bonus** — matches natural-language hints like `"early May"`, `"end of April"`: +10 to +15.
- **Urgency decay** — mild pull toward the near term so the backlog keeps moving.
- **Spread penalty (picking)** — short activities must be ≥ 2 days apart in the top-3; multi-day activities must be ≥ 7 days apart so you don't get three back-to-back weekend proposals.

The scorer is deterministic: same input JSON + same weather cache + same data files → same `suggested_dates`.

### Inspecting scores

Use `--explain <id>` to see the full per-day breakdown for a single entry:

```
python3 scripts/analyze.py --explain act_002
```

## Reply handling

When the user replies to a Telegram suggestion, apply these mutations to `~/activity-ideas.json`:

| reply | effect |
| --- | --- |
| `1`, `2`, `3`... | Set activity N's `status` to `scheduled`; replace `suggested_dates` with the single accepted date |
| ISO date or `Tue, May 26` | Override: `status` → `scheduled`, `suggested_dates` → `[parsed_date]` |
| `all` | Accept top suggestion for every pending item; bulk set `scheduled` |
| `skip` | Leave `status=pending`; append today to an internal `skipped_on` list so next analysis de-prioritizes |
| `edit` | Ask the user which activity and what to change |
| `history` | Print last 10 entries with `status=completed`; no JSON mutation |

There is no inbound webhook — replies are handled when the user types them into the Hermes chat, not when they reply inside Telegram. (If you want true inbound routing, add a Telegram webhook to a separate service; it's out of scope for this skill.)

## Telegram message format

`scripts/notify.py` produces (Markdown V2 escaping handled by the script). The top section surfaces everything already on the books whose date (or date range) is still in the future — it is kept small but visually separated from the suggestions below by a heavy horizontal rule so the user can't miss what's already committed. Multi-day activities render as ranges and annotate any adjacent company holiday:

```
📅 *Activity Date Suggestions*

✅ *Upcoming — already scheduled:*
• Morning Yoga — Tue, May 26
• Nashville trip — Sat, May 23 → Mon, May 25 (Memorial Day)

━━━━━━━━━━━━━━━━━━

📝 *Suggestions — reply with the number to accept:*

1. *Learn Spanish vocabulary* — Mon, Apr 27
   learning · indoor · 60-90 min · morning

2. *Hiking at Zermatt* — Sat, May 2
   outdoor · 2-3 hours · daytime · ☀️ rain 10% / cloud 30%

3. *Cook Italian dinner* — Sat, Apr 25
   social · indoor · 3 hours · evening

Reply: number to accept · date to override · all · skip · history
```

The *Upcoming* section is omitted entirely when no scheduled activity is still in the future; likewise the divider is only rendered when both sections are present. A multi-day range stays listed until its last day has passed (so a trip that started yesterday is still "upcoming" until it ends).

## Troubleshooting

- **`analyze.py` exits with KeyError** — a legacy entry is missing a required field. Run `python3 scripts/analyze.py --migrate` to fill defaults.
- **Telegram 401 Unauthorized** — `TELEGRAM_BOT_TOKEN` not exported into the cron environment. Cron does **not** source `~/.zshrc`, `~/.bashrc`, or any shell profile — it starts a minimal environment. Set the vars directly in the crontab:
  ```
  0 7,19 * * * TELEGRAM_BOT_TOKEN=123456:ABC... TELEGRAM_CHAT_ID=12345678 cd ~/.hermes/skills/productivity/activity-tracker && python3 scripts/analyze.py && python3 scripts/notify.py
  ```
  `notify.py` has a built-in fallback that reads from `~/.hermes/.env` if env vars are unset, so this is no longer needed. Just set the cron directly:
  ```
  0 7,19 * * * cd ~/.hermes/skills/productivity/activity-tracker && python3 scripts/analyze.py && python3 scripts/notify.py
  ```
- **Cron token loading quirk**: `notify.py` loads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_HOME_CHANNEL` from `~/.hermes/.env` via a fallback in `send()`. The `.env` file may display tokens as masked (`***`) in tool outputs, but the full bytes are stored. The `_load_env_token` function checks `val != "***"` (literal) so real tokens starting with a number like `8709246245:...` pass through correctly. If you edit `notify.py` and break the `startswith("TELEGRAM_BOT_TOKEN=")` check, ensure the string literal is not truncated — a common patching error leaves the quote unbalanced.
- **`chanceofrain` parse error** — wttr.in returns strings; the parser calls `int()` on them. If you see a `ValueError`, wttr.in changed its schema — add a `try/except` around the coercion and log the offending value.

## Files

```
activity-tracker/
├── SKILL.md
├── data/
│   ├── holidays.json        # company holidays (update once a year)
│   └── work_schedule.json   # weekly WFH/office/off pattern + hours
└── scripts/
    ├── analyze.py           # scoring + JSON mutation
    ├── weather.py           # wttr.in wrapper with caching
    ├── schedule.py          # holiday/work-schedule helpers (shared)
    └── notify.py            # Telegram formatter + sender
```
