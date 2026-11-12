# Tray API

Base URL: `http://rfid-pi.local:8000` (or the Pi's IP; port 8000).

`/api/v1/` is the stable API for other systems: **read-only**, local network only
(private IPv4 ranges and Tailscale `100.64.0.0/10`; anything else gets `403`), with CORS on
(`Access-Control-Allow-Origin: *`, preflight allows the `Authorization` header), so a page on another device can read it.
All times are ISO 8601 in Europe/Vienna, e.g. `2026-11-11T20:00:00+01:00`.

| Method | Path | Access | What |
|---|---|---|---|
| GET | `/api/v1/status` | open | Overall state, what's missing, next event |
| GET | `/api/v1/health` | open | Calendar and weather freshness |
| GET | `/api/v1/items` | **token** | Every known item: expected today, on the tray, which reader |
| GET | `/api/v1/plan` | **token** | Today's events (titles!), each item with the reason it's needed, the day's required list, weather reminders and summary |
| GET | `/api/v1/stream` | **token** or `/display` session | Server-Sent Events: the status now, then on every change |
| GET | `/display` | open | Full-screen status page for a tablet / second screen (`home-display.html`) |

## The token

Protected endpoints need `Authorization: Bearer <api_token>`; without it (or with a wrong one) they answer
`401 {"error": "Bearer token required"}`. The tray creates a random `api_token` in `/home/pi/tray_config.json`
(mode 600) on its first start if there is none. To read it:

```bash
ssh pi@rfid-pi.local "python3 -c 'import json; print(json.load(open(\"/home/pi/tray_config.json\"))[\"api_token\"])'"
TOKEN=...   # paste it; keep it out of scripts you publish
```
To change it: put a new value in `api_token` (or delete the key to get a fresh random one) and
`sudo systemctl restart tray`. Old tokens and old display sessions stop working.

**`/display` and the session cookie.** A browser's `EventSource` can't send an `Authorization` header, so
when the Pi serves `/display` to a device on the local network it also sets a cookie
`tray_session` (`HttpOnly`, `SameSite=Strict`, `Path=/api/v1/stream`, one year). Its value is derived from
the token (HMAC), not the token itself, the page never sees the token, and it only opens `/api/v1/stream`:
`/plan` and `/items` still need the real token. Open the display as `http://<pi>:8000/display` on the
tablet; a copy of `home-display.html` opened from anywhere else can't read the stream.

## Overall state

Checked against the **plan in focus** (`plan_for`): normally today's plan, the items of all of today's events
that haven't ended yet (the same list "I'm leaving" checks). Between `evening_from` and `evening_until`
(tray_config.json, default `"18:00"` / `"03:00"`) it is **tomorrow's** plan instead; after midnight "tomorrow"
means the coming day, since the evening began the day before. First match wins:

| State | When | LEDs (green ACT / red PWR) |
|---|---|---|
| `MISSING` | Something in the plan isn't on the tray, or has no tag | green off, red on |
| `STALE` | Calendar data older than `stale_after_hours` (tray_config.json, default 6), or none at all after a failed fetch | green on, red blinks 1 Hz |
| `IDLE` | No event with items left today | both off |
| `READY` | Everything in the plan is on the tray | green on, red off |

The last good calendar is kept in `/home/pi/calendar_cache.ics` (+ `calendar_cache.json` with `fetched_at`),
loaded at startup, so the plan survives an offline boot. A bad response (network or HTTP error, empty,
not iCal) never replaces it.

## GET /api/v1/status

```bash
curl -s http://rfid-pi.local:8000/api/v1/status
```
```json
{"state": "MISSING", "plan_for": "today", "missing": ["towel"], "no_tag": [],
 "reminders": [{"name": "umbrella", "reason": "rain 70% at 08:00"}], "weather_simulated": null, "weather_unavailable": false,
 "next_event": {"title": "Gym", "start": "2026-11-11T20:00:00+01:00", "end": "2026-11-11T21:00:00+01:00"},
 "time": "2026-11-11T16:30:00+01:00"}
```
```json
{"type": "object", "required": ["state", "plan_for", "missing", "no_tag", "next_event", "time"],
 "properties": {
  "state": {"enum": ["READY", "MISSING", "IDLE", "STALE"]},
  "plan_for": {"enum": ["today", "tomorrow"], "description": "tomorrow between evening_from and evening_until"},
  "missing": {"type": "array", "items": {"type": "string"}, "description": "tagged items not on the tray"},
  "no_tag": {"type": "array", "items": {"type": "string"}, "description": "needed items with no tag: can never be on the tray"},
  "reminders": {"type": "array", "description": "weather suggestions not on the tray right now: shown, never MISSING (tagged or not)",
   "items": {"type": "object", "properties": {"name": {"type": "string"}, "reason": {"type": "string"}}}},
  "weather_simulated": {"enum": [null, "rain", "hot", "cold", "off"], "description": "weather_override when it isn't \"none\""},
  "weather_unavailable": {"type": "boolean", "description": "forecast older than weather_stale_hours (or none for the plan window): no weather items"},
  "next_event": {"type": ["object", "null"], "description": "first event with items in the plan in focus that hasn't ended (may be in progress)",
   "properties": {"title": {"type": "string"}, "start": {"type": "string", "format": "date-time"},
                  "end": {"type": "string", "format": "date-time"}}},
  "time": {"type": "string", "format": "date-time"}}}
```

## GET /api/v1/items

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://rfid-pi.local:8000/api/v1/items
```
```json
{"items": [
  {"name": "bottle", "expected": true, "on_tray": true, "slot": "R2", "tagged": true},
  {"name": "keys", "expected": false, "on_tray": false, "slot": null, "tagged": true},
  {"name": "umbrella", "expected": true, "on_tray": false, "slot": null, "tagged": false}],
 "time": "2026-11-11T16:30:00+01:00"}
```
```json
{"type": "object", "required": ["items", "time"], "properties": {
  "items": {"type": "array", "items": {"type": "object",
   "required": ["name", "expected", "on_tray", "slot", "tagged"], "properties": {
    "name": {"type": "string"},
    "expected": {"type": "boolean", "description": "in the plan in focus (tomorrow's in the evening)"},
    "on_tray": {"type": "boolean"},
    "slot": {"type": ["string", "null"], "enum": ["R1", "R2", "R3", null], "description": "reader that sees it now"},
    "tagged": {"type": "boolean", "description": "false = needed today but no tag has this name"}}}},
  "time": {"type": "string", "format": "date-time"}}}
```
Items: every named tag, plus needed items that have no tag. The reader (`slot`) is informative only:
items can sit anywhere on the tray.

## GET /api/v1/plan

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://rfid-pi.local:8000/api/v1/plan
```
```json
{"date": "2026-11-11",
 "events": [
  {"title": "Work", "start": "2026-11-11T09:00:00+01:00", "end": "2026-11-11T17:00:00+01:00", "done": true,
   "items": [{"name": "laptop", "reasons": ["preset:work"]}, {"name": "bottle", "reasons": ["preset:work"]}]},
  {"title": "Gym", "start": "2026-11-11T20:00:00+01:00", "end": "2026-11-11T21:00:00+01:00", "done": false,
   "items": [{"name": "bottle", "reasons": ["preset:gym", "notes"]}, {"name": "towel", "reasons": ["preset:gym"]}]}],
 "required": [{"name": "bottle", "status": "on_tray"}, {"name": "towel", "status": "missing"}],
 "reminders": [{"name": "umbrella", "reason": "rain 70% at 20:00"}],
 "weather": {"simulated": null, "note": null,
  "now": {"temperature_2m": 6.1, "apparent_temperature": 3.9, "precipitation_probability": 40, "precipitation": 0.0, "cloud_cover": 100, "uv_index": 0.0},
  "window": {"from": "2026-11-11T20:00:00+01:00", "to": "2026-11-11T21:00:00+01:00",
             "min": {"temperature_2m": 5.2, "...": 0}, "max": {"precipitation_probability": 70, "...": 0}},
  "items": [{"name": "umbrella", "reason": "rain 70% at 20:00", "rule": "rain", "tagged": false, "on_tray": false}],
  "reasons": ["umbrella: rain 70% at 20:00"]},
 "time": "2026-11-11T18:00:00+01:00"}
```
```json
{"type": "object", "required": ["date", "events", "required", "time"], "properties": {
  "date": {"type": "string", "format": "date"},
  "events": {"type": "array", "description": "all of today's timed events, finished ones too", "items": {"type": "object",
   "required": ["title", "start", "end", "done", "items"], "properties": {
    "title": {"type": "string"}, "start": {"type": "string", "format": "date-time"},
    "end": {"type": "string", "format": "date-time"},
    "done": {"type": "boolean", "description": "ended: no longer counts towards required"},
    "items": {"type": "array", "items": {"type": "object", "required": ["name", "reasons"], "properties": {
     "name": {"type": "string"},
     "reasons": {"type": "array", "items": {"type": "string"}, "description":
      "\"preset:<name>\" = the title matches that list; \"notes\" = Bring:/Porta: line in the event notes; \"override\" = list set for this event only"}}}}}}},
  "required": {"type": "array", "description": "today's plan: items of events not over yet", "items": {"type": "object",
   "required": ["name", "status"], "properties": {"name": {"type": "string"},
    "status": {"enum": ["on_tray", "missing", "no_tag"]}}}},
  "reminders": {"type": "array", "description": "as in /api/v1/status, for today's plan"},
  "weather": {"type": "object", "properties": {
   "simulated": {"enum": [null, "rain", "hot", "cold", "off"]},
   "note": {"enum": [null, "off", "unavailable"], "description": "why no weather rules apply"},
   "now": {"type": ["object", "null"], "description": "this hour: temperature_2m, apparent_temperature (°C), precipitation_probability (%), precipitation (mm), cloud_cover (%), uv_index"},
   "window": {"type": ["object", "null"], "description": "plan window (first start .. last end of the plan's events) with min/max of each value"},
   "items": {"type": "array", "description": "weather suggestions: name, reason, rule, tagged, on_tray"},
   "reasons": {"type": "array", "items": {"type": "string"}, "description": "e.g. \"scarf, gloves: 3 degrees at 08:00\""}}},
  "time": {"type": "string", "format": "date-time"}}}
```
Weather items come from the rules in `tray_rules.json` (edit them in Settings, Weather rules) and the Open-Meteo forecast.
They are only suggestions: never in `required`, never MISSING, even with a tag.

## GET /api/v1/health

```bash
curl -s http://rfid-pi.local:8000/api/v1/health
```
```json
{"calendar": {"state": "ok", "last_updated": "2026-11-11T16:29:41+01:00", "age_minutes": 0.3, "stale": false},
 "weather": {"fetched_at": "2026-11-11T16:05:12+01:00", "age_minutes": 25.1, "stale": false},
 "time": "2026-11-11T16:30:00+01:00"}
```
```json
{"type": "object", "required": ["calendar", "weather", "time"], "properties": {
  "calendar": {"type": "object", "required": ["state", "last_updated", "age_minutes", "stale"], "properties": {
   "state": {"enum": ["ok", "loading", "unreachable", "invalid", "unconfigured"]},
   "last_updated": {"type": ["string", "null"], "format": "date-time", "description": "last successful fetch (every 60 s)"},
   "age_minutes": {"type": ["number", "null"]},
   "stale": {"type": "boolean", "description": "older than stale_after_hours (state is STALE unless something is MISSING)"}}},
  "weather": {"type": "object", "description": "no location and no weather values here", "properties": {
   "fetched_at": {"type": ["string", "null"], "format": "date-time", "description": "last good forecast (every weather_refresh_minutes)"},
   "age_minutes": {"type": ["number", "null"]},
   "stale": {"type": "boolean", "description": "older than weather_stale_hours: weather rules are ignored"}}},
  "time": {"type": "string", "format": "date-time"}}}
```

## GET /api/v1/stream (Server-Sent Events)

Sends the `/api/v1/status` object straight away, then again every time it changes (state, missing
items or next event). A `: ping` comment every 15 s keeps the connection open.

```bash
curl -sN -H "Authorization: Bearer $TOKEN" http://rfid-pi.local:8000/api/v1/stream
```
```
data: {"state": "READY", "missing": [], "no_tag": [], "next_event": {...}, "time": "..."}

: ping

data: {"state": "MISSING", "missing": ["towel"], ...}
```
In a page served by the Pi (`/display`), `new EventSource("/api/v1/stream")` works with the session cookie.
From another origin, `EventSource` can't send the token: poll `/api/v1/status` (open), or read the stream with
`fetch("http://rfid-pi.local:8000/api/v1/stream", {headers: {Authorization: "Bearer " + token}})` and its body reader.

## Writes

There are no `/api/v1` endpoints that change data yet. Any `POST`/`DELETE` under `/api/v1/` must send
the Bearer token (the display session doesn't count):

```bash
curl -s -X POST http://rfid-pi.local:8000/api/v1/status                               # 401 {"error": "Bearer token required"}
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://rfid-pi.local:8000/api/v1/status   # 405 {"error": "read-only"}
```

## Older endpoints (used by the web page, not stable)

These stay as they are for `ui.html`. They are not part of the stable API: no CORS, the shape can change.
**Note:** they have no token (only the optional `PIN`), and `/api/state` includes today's event titles too.
Writes need `Content-Type: application/json`, so a page on another site can't send them.

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | `/` | | the web page |
| GET | `/api/state` | | everything the page shows: `on_tray`, `names`, `tags`, `unnamed`, `today`, `day`, `events`, `next`, `log`, `cal`, `calendar_updated` (ISO or null), `age_minutes` (int or null), `stale` (bool), `ntfy`, `ics`, `lead`, `plan_for`, `focus_date` (ISO date of the plan in focus), `tomorrow` (`{keys, items}` in the evening, else null), `bag` (uid or null), `now` (epoch), `simulated` (ISO start of the simulated clock, or null), `weather` (`{override, simulated, note, today: [weather items], tomorrow: [weather items] or null, location: {name, latitude, longitude}, now: {this hour} or null, age_minutes, range: {from, to, min_<var>, max_<var>} over the plan in focus or null}`; the location is only here, never in `/api/v1`) |
| GET | `/api/rules` | | `{"rules": [{"name", "condition", "items"}], "vars": [...], "override"}` |
| POST | `/api/rules` | `{"rules": [{"name", "condition", "items"}]}` (whole list, at most 20) | `{"ok", "msg"}` |
| GET | `/api/presets` | | `{"presets": [{"name", "aliases", "items"}]}` |
| POST | `/api/leave` | `{}` | `{"problems": [...], "event": "Work 09:00 + Gym 20:00" \| null}` (may send a notification) |
| POST | `/api/name` | `{"uid", "name"}` | `{"ok": true}` |
| POST | `/api/delete` | `{"uid"}` | `{"ok": true}` |
| POST | `/api/bag` | `{"uid"}` (`""` = no bag) | `{"ok": true}` |
| POST | `/api/event_items` | `{"key", "scope": "event"\|"reset"\|"preset", "items", "preset"?}` | `{"ok", "msg"}` |
| POST | `/api/presets` | `{"name", "aliases", "items", "old_name"?}` | `{"ok", "msg"}` |
| DELETE | `/api/presets` | `{"name"}` | `{"ok", "msg"}` |
| GET | `/api/geocode?q=Villach` | | `{"ok", "msg", "results": [{"name", "region", "latitude", "longitude"}]}` (Open-Meteo geocoding, for Settings > Location) |
| POST | `/api/demo_time` | `{"fake_now": "2026-11-19T19:00"}` (`""` = real time) | `{"ok", "msg"}`: simulated clock at once, in memory only (a restart goes back to real time) |
| POST | `/api/settings` | `{"ntfy_topic"?, "ics_url"?, "lead_minutes"?, "weather_override"?, "latitude"? + "longitude"? (+ "location_name"?)}` | `{"ok", "msg"}` |
| POST | `/api/test_notify` | `{}` | `{"ok", "msg"}` |
