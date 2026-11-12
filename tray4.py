import sys, os, re, csv, time, json, queue, select, socket, threading, html, hmac, base64, ipaddress, secrets, operator, http.cookies, urllib.parse, urllib.request
from collections import deque
from datetime import datetime, timedelta, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo
import icalendar, recurring_ical_events
import led

BASE_URL = ""   # optional, e.g. "http://100.x.y.z:8000" when using Tailscale
DATA_FILE = "/home/pi/tray_data.json"
# secrets: ntfy_topic, ics_url, api_token (chmod 600). TRAY_CONFIG: other path, so tests never read the real one
CONFIG_FILE = os.environ.get("TRAY_CONFIG", "/home/pi/tray_config.json")
CACHE_ICS = "/home/pi/calendar_cache.ics"    # last good calendar, survives restarts and offline boots
CACHE_META = "/home/pi/calendar_cache.json"  # {"fetched_at": epoch}
# next to the config, so tests (TRAY_CONFIG in a temp folder) never touch the real ones
RULES_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "tray_rules.json")         # weather rules, edited in the page
WEATHER_CACHE = os.path.join(os.path.dirname(CONFIG_FILE), "weather_cache.json")   # last good forecast + fetched_at
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"  # free, no key
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"  # city name -> coordinates, free, no key
PIN = ""        # optional: if set, the page asks for any username + this PIN
PORT = 8000
TIMEOUT = 2.5
LIFT_WINDOW = 1.5  # s: tags that all leave within this = the bag was lifted (tune on the real tray)
LIFT_MIN = 2       # fewer tags than this going at once is just one thing taken off
EVENING_SETTLE = 7   # s: no bag tag set -> check once the tray hasn't changed for this long
BAG_SETTLE = 3       # s: bag tag placed -> wait this long for the things in it to be read too
CAL_REFRESH = 60
TAILNET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale addresses count as local for /api/v1 reads
EVENTS_FILE = "/home/pi/events.csv"  # slot / state / notify rows for the test session (real time, ms)
EVENTS_MAX = 5 * 1024 * 1024         # then events.csv -> events.csv.1 (one old file kept)
DISPLAY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home-display.html")
TZ = ZoneInfo("Europe/Vienna")  # displayed times and "floating" calendar times
SLOTS = ("R1", "R2", "R3")  # prototype readers; only which tags are read matters, not where
UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

lock = threading.Lock()
try:
    with open(DATA_FILE) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}  # any other error (broken JSON) stops the script: never overwrite the data
if "tags" not in data:
    data["tags"] = {"33:46:64:F5": "keys", "2E:97:29:03": "wallet"}
# "slots" (expected item per reader) is no longer used: items can go anywhere on the tray
data.setdefault("unnamed", {})
data.setdefault("notified", {})
data.setdefault("presets", {"gym": {"aliases": ["sport"], "items": ["bottle", "towel"]},
                            "work": {"aliases": [], "items": ["pc", "bottle"]}})
data.setdefault("event_overrides", {})  # "<uid>|<YYYY-MM-DD>": [items] for that occurrence only

config = {"ntfy_topic": "", "ics_url": "", "lead_minutes": 15}
try:
    with open(CONFIG_FILE) as f:
        config.update(json.load(f))
except FileNotFoundError:
    pass

def save():
    with open(DATA_FILE + ".tmp", "w") as f:
        json.dump(data, f, indent=2)
    os.replace(DATA_FILE + ".tmp", DATA_FILE)

def save_config():
    tmp = CONFIG_FILE + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)

def lead_minutes():
    try:
        return int(config.get("lead_minutes", 15))
    except (TypeError, ValueError):
        return 15

def stale_after():
    """Seconds before calendar data counts as stale (stale_after_hours in the config, default 6)."""
    try:
        return float(config.get("stale_after_hours", 6)) * 3600
    except (TypeError, ValueError):
        return 6 * 3600

def masked(v):
    return {"set": bool(v), "last4": v[-4:] if v else ""}

logbuf = deque(maxlen=60)

def now_str(fmt="%d/%m %H:%M:%S"):
    return datetime.now(TZ).strftime(fmt)

def log(msg):
    line = now_str() + " " + msg
    print(line)
    logbuf.append(line)

fake = {"offset": 0.0, "at": ""}  # simulated clock (TRAY_FAKE_NOW, else fake_now in the config)

def set_fake_now(value):
    """Simulated time: the clock starts at `value` (ISO; Vienna time if no offset) and runs on from there."""
    value = (value or "").strip()
    if not value:
        return fake.update(offset=0.0, at="")
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ)
    fake.update(offset=t.timestamp() - time.time(), at=value)

def clock():
    """Now, for all tray logic (simulated when fake_now / TRAY_FAKE_NOW is set)."""
    return time.time() + fake["offset"]

try:
    set_fake_now(os.environ.get("TRAY_FAKE_NOW") or config.get("fake_now", ""))
except ValueError as e:
    print(f"fake_now ignored, not an ISO time: {e}")

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:  # booted without network: don't crash, the name works once Wi-Fi is back
        return socket.gethostname() + ".local"
    finally:
        s.close()

# ---------- event log (~/events.csv) ----------

EVENT_COLUMNS = ("ts", "type", "slot", "name", "state", "detail")
events_q = None  # a Queue once main() starts the writer; before that (tests, tools) nothing is written

def log_event(type_, slot="", name="", state="", detail=""):
    """Never blocks the reader loop: the row goes on a queue, events_writer() writes it."""
    if events_q is not None:
        events_q.put((time.time(), type_, slot, name, state, detail))

def events_writer():
    """Writes queued rows in batches, real time with ms; rotates past EVENTS_MAX. Never secrets: only
    slot changes, states and notification texts (which hold item names and event titles)."""
    warned = False
    while True:
        rows = [events_q.get()]
        while len(rows) < 500:
            try:
                rows.append(events_q.get_nowait())
            except queue.Empty:
                break
        try:
            if os.path.exists(EVENTS_FILE) and os.path.getsize(EVENTS_FILE) > EVENTS_MAX:
                os.replace(EVENTS_FILE, EVENTS_FILE + ".1")
            new = not os.path.exists(EVENTS_FILE)
            with open(EVENTS_FILE, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(EVENT_COLUMNS)
                w.writerows([datetime.fromtimestamp(r[0], TZ).isoformat(timespec="milliseconds"), *r[1:]]
                            for r in rows)
        except OSError as e:
            if not warned:
                warned = True
                log(f"events.csv not written ({e})")

def notify(msg, click=None):
    log("NOTIFY: " + msg)
    topic = config.get("ntfy_topic")
    if not topic:
        log_event("notify", detail=msg + " [not sent: no ntfy topic]")
        return False
    try:
        req = urllib.request.Request("https://ntfy.sh/" + topic,
                                     data=msg.encode("utf-8"), method="POST")
        req.add_header("Title", "Tray RFID")
        if click:
            req.add_header("Click", click)
        urllib.request.urlopen(req, timeout=5)
        log_event("notify", detail=msg + " [sent]")
        return True
    except Exception as e:
        log(f"ntfy failed: {e}".replace(topic, "<topic>"))
        log_event("notify", detail=msg + " [send failed]")
        return False

def fmt(t, day=True):
    d = datetime.fromtimestamp(t, TZ)
    return (DAYS[d.weekday()] + d.strftime(" %d/%m ") if day else "") + d.strftime("%H:%M")

def day_of(t):
    return datetime.fromtimestamp(t, TZ).date()

def start_of_day(t):
    return datetime.combine(day_of(t), dtime.min, TZ).timestamp()

# ---------- ICS calendar ----------

def bring_line(desc):
    """'Bring: a, b' and 'Travel: N' lines in the notes (Italian 'Porta:'/'Viaggio:' too) -> (items, minutes)."""
    desc = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", desc)  # Google sometimes stores notes as HTML
    desc = html.unescape(re.sub(r"<[^>]+>", "", desc))
    items, travel = [], 0
    for line in desc.splitlines():
        m = re.match(r"\s*(?:bring|porta)\s*:(.*)", line, re.I)
        if m:
            items = [x.strip() for x in m.group(1).split(",") if x.strip()]
        m = re.match(r"\s*(?:travel|viaggio)\s*:\s*(\d+)", line, re.I)
        if m:
            travel = int(m.group(1))
    return items, travel

def parse_ics(text, lo, hi):
    """Timed events starting between lo and hi: [{uid, title, start, end, bring, travel}]."""
    cal_ = icalendar.Calendar.from_ical(text)
    out = []
    for e in recurring_ical_events.of(cal_).between(datetime.fromtimestamp(lo, TZ),
                                                     datetime.fromtimestamp(hi, TZ)):
        start = e.get("DTSTART").dt
        if not isinstance(start, datetime) or str(e.get("STATUS", "")).upper() == "CANCELLED":
            continue  # ponytail: all-day events ignored (no time to notify before)
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        end = e["DTEND"].dt if "DTEND" in e else start + e["DURATION"].dt if "DURATION" in e else start
        if not isinstance(end, datetime):
            end = start
        elif end.tzinfo is None:
            end = end.replace(tzinfo=TZ)
        items, travel = bring_line(str(e.get("DESCRIPTION", "")))
        if lo <= start.timestamp() <= hi:
            out.append({"uid": str(e.get("UID", "")), "title": str(e.get("SUMMARY", "")).strip()
                        or "(untitled)", "start": start.timestamp(), "end": end.timestamp(), "bring": items, "travel": travel})
    return sorted(out, key=lambda x: x["start"])

# state: unconfigured | loading | unreachable | invalid | ok
cal = {"state": "loading", "events": [], "updated": None, "next_fetch": 0}
wake = threading.Event()

def set_cal(state, err=None, url=""):
    if state != cal["state"]:
        detail = f" ({err})".replace(url, "<url>") if err and url else ""
        log(f"Calendar: {state}{detail}")
    cal["state"] = state

def item_status(items, now, on_tray=None):
    """on_tray: lowercase names to check against (default: what's on the tray now)."""
    with lock:
        names = {n.lower() for n in data["tags"].values()}
    if on_tray is None:
        on_tray = {(n or "").lower() for _, n in tray_tags(now)}
    return [{"name": i, "status": "ok" if i.lower() in on_tray
             else "missing" if i.lower() in names else "unknown"} for i in items]

# ---------- item lists per event and per day ----------

def event_key(ev):
    return f'{ev["uid"]}|{day_of(ev["start"]).isoformat()}'

def word_in(word, title):
    word = word.strip()
    return bool(word) and re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", title, re.I) is not None

def matching_presets(title):  # call with lock held
    return [n for n, p in data["presets"].items() if any(word_in(w, title) for w in [n, *p.get("aliases", [])])]

def find_preset(word):  # call with lock held; by name or alias, case-insensitive
    w = word.strip().lower()
    return next((n for n, p in data["presets"].items()
                 if w and (n.lower() == w or w in (a.lower() for a in p.get("aliases", [])))), None)

def dedupe(items):
    seen, out = set(), []
    for i in items:
        i = str(i).strip()[:30]
        if i and i.lower() not in seen:
            seen.add(i.lower())
            out.append(i)
    return out

def resolve(ev):
    """-> (items, override?, presets used): the override if any, else title presets + 'Bring:'/'Porta:' line."""
    with lock:
        ov = data["event_overrides"].get(event_key(ev))
        if ov is not None:
            return list(ov), True, []
        names = matching_presets(ev["title"])
        items = [i for n in names for i in data["presets"][n]["items"]]
    return dedupe(items + ev["bring"]), False, names

def plan_events(day, after):
    """Events with items on `day` that aren't over yet at `after` (ongoing or ahead)."""
    return [e for e in cal["events"] if max(e["start"], e.get("end", 0)) >= after
            and day_of(e["start"]) == day and resolve(e)[0]]

def rest_of_day(after):
    """Everything to carry from `after` on, today."""
    return plan_events(day_of(after), after)

def minutes_of(key, default):
    try:
        h, m = map(int, str(config.get(key, default)).split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except ValueError:
        pass
    h, m = map(int, default.split(":"))
    return h * 60 + m

def focus_day(now):
    """-> ("tomorrow", day) between evening_from and evening_until, else ("today", day).
    After midnight "tomorrow" is the coming day, i.e. today's date: the evening began yesterday."""
    d = datetime.fromtimestamp(now, TZ)
    t = d.hour * 60 + d.minute
    a, b = minutes_of("evening_from", "18:00"), minutes_of("evening_until", "03:00")
    if not ((a <= t or t < b) if a > b else (a <= t < b)):
        return "today", d.date()
    return "tomorrow", d.date() + timedelta(days=1) if t >= a else d.date()

def focus_plan(now):
    """-> (plan_for, events): the plan the LEDs, the status and the evening check look at."""
    plan_for, day = focus_day(now)
    return plan_for, plan_events(day, now)

def first_of_day(t):
    """The day's first event with items: its reminder is the day's only one and covers the whole day."""
    return next((e for e in cal["events"] if day_of(e["start"]) == day_of(t) and resolve(e)[0]), None)

def remind_at(ev):
    return ev["start"] - (lead_minutes() + ev["travel"]) * 60

def day_items(events):
    """The plan's items: lists, notes and overrides. Weather items are never in it: they're only suggestions."""
    return dedupe([i for e in events for i in resolve(e)[0]])

def label(events):
    return " + ".join(f'{e["title"]} {fmt(e["start"], False)}' for e in events)

# ---------- weather (Open-Meteo) and weather rules ----------

WEATHER_VARS = ("temperature_2m", "apparent_temperature", "precipitation_probability", "precipitation",
                "cloud_cover", "uv_index")
DEFAULT_RULES = [
    {"name": "rain", "condition": "precipitation_probability >= 50 or precipitation >= 0.5", "items": ["umbrella"]},
    {"name": "hot_sunny", "condition": "apparent_temperature >= 28 and cloud_cover <= 50", "items": ["sunglasses"]},
    {"name": "strong_sun", "condition": "uv_index >= 6", "items": ["sunscreen"]},
    {"name": "cold", "condition": "temperature_2m <= 5", "items": ["scarf", "gloves"]},
]
SIM_WEATHER = {  # weather_override: the same hour, every hour (demo and tests)
    "rain": {"temperature_2m": 9, "apparent_temperature": 7, "precipitation_probability": 80, "precipitation": 2.0,
             "cloud_cover": 100, "uv_index": 1},
    "hot": {"temperature_2m": 31, "apparent_temperature": 33, "precipitation_probability": 0, "precipitation": 0,
            "cloud_cover": 10, "uv_index": 8},
    "cold": {"temperature_2m": 2, "apparent_temperature": -2, "precipitation_probability": 10, "precipitation": 0,
             "cloud_cover": 60, "uv_index": 1},
}
OVERRIDES = ("none", "rain", "hot", "cold", "off")
OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
CLAUSE = re.compile(r"(\w+)\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*(?:%|mm|°c?|degrees)?", re.I)
SAY = {"precipitation_probability": "rain {}%", "precipitation": "rain {} mm", "temperature_2m": "{} degrees",
       "apparent_temperature": "feels like {} degrees", "cloud_cover": "clouds {}%", "uv_index": "UV {}"}
weather = {"hours": [], "fetched_at": None}  # hours: [{"t": epoch of the hour, var: value}]

def num(key, default):
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default

def weather_override():
    v = str(config.get("weather_override", "none")).strip().lower()
    return v if v in OVERRIDES else "none"

def parse_condition(text):
    """'a >= 50 or b >= 0.5 and c <= 3' -> OR of AND groups [[(var, op, value)]]. ValueError if unreadable."""
    groups = []
    for part in re.split(r"\s+or\s+", str(text).strip(), flags=re.I):
        g = []
        for c in re.split(r"\s+and\s+", part.strip(), flags=re.I):
            m = CLAUSE.fullmatch(c.strip())
            if not m or m.group(1).lower() not in WEATHER_VARS:
                raise ValueError(f"can't read “{c.strip()[:40]}” (write e.g. uv_index >= 6)")
            g.append((m.group(1).lower(), m.group(2), float(m.group(3))))
        groups.append(g)
    return groups

def clean_rules(lst):
    """Validated copy of a rule list, or ValueError with a message for the page."""
    if not isinstance(lst, list) or len(lst) > 20:
        raise ValueError("Invalid rules (at most 20)")
    out = []
    for r in lst:
        if not isinstance(r, dict):
            raise ValueError("Invalid rule")
        name = str(r.get("name", "")).strip()[:30]
        items = r.get("items", [])
        items = dedupe(items.split(",") if isinstance(items, str) else items if isinstance(items, list) else [])[:10]
        if not name or not items:
            raise ValueError(f"Rule “{name or '?'}”: give it a name and at least one item")
        try:
            parse_condition(str(r.get("condition", ""))[:200])
        except ValueError as e:
            raise ValueError(f"Rule “{name}”: {e}")
        out.append({"name": name, "condition": str(r["condition"]).strip()[:200], "items": items})
    return out

def load_rules():
    try:
        with open(RULES_FILE) as f:
            return clean_rules(json.load(f))
    except FileNotFoundError:
        return [dict(r) for r in DEFAULT_RULES]
    except (OSError, ValueError) as e:
        log(f"tray_rules.json not usable ({e}): default weather rules")
        return [dict(r) for r in DEFAULT_RULES]

rules = load_rules()

def save_rules(b):
    try:
        new = clean_rules(b.get("rules"))
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    with lock:
        rules[:] = new
        write_private(RULES_FILE, json.dumps(new, indent=2).encode())
    log(f"Weather rules saved ({len(new)})")
    return {"ok": True, "msg": "Rules saved"}

def weather_hours(hourly):
    """Open-Meteo 'hourly' block -> [{"t", var: value}]. Raises on a malformed response."""
    times = hourly["time"]
    cols = {v: hourly[v] for v in WEATHER_VARS}
    if any(len(c) != len(times) for c in cols.values()):
        raise ValueError("hourly columns of different lengths")
    val = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else None
    return [{"t": datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp(), **{v: val(cols[v][i]) for v in WEATHER_VARS}}
            for i, s in enumerate(times)]

def fetch_weather():
    """One fetch; only a good response replaces the data and the cache. Never logs the URL (coordinates)."""
    q = urllib.parse.urlencode({"latitude": num("latitude", 46.62), "longitude": num("longitude", 14.31),
                                "hourly": ",".join(WEATHER_VARS), "timezone": "Europe/Vienna", "forecast_days": 3})
    try:
        req = urllib.request.Request(WEATHER_URL + "?" + q, headers={"User-Agent": "tray-rfid"})
        with urllib.request.urlopen(req, timeout=10) as r:
            hourly = json.load(r)["hourly"]
        hours = weather_hours(hourly)
    except Exception as e:
        log(f"Weather: fetch failed ({type(e).__name__}), keeping the last forecast")
        return False
    now = time.time()
    weather.update(hours=hours, fetched_at=now)
    try:  # only the hourly values: no coordinates in the cache
        write_private(WEATHER_CACHE, json.dumps({"fetched_at": now, "hourly": hourly}).encode())
    except OSError as e:
        log(f"Weather cache not saved ({type(e).__name__})")
    return True

def load_weather_cache():
    try:
        with open(WEATHER_CACHE) as f:
            j = json.load(f)
        weather.update(hours=weather_hours(j["hourly"]), fetched_at=float(j["fetched_at"]))
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"Weather cache unreadable, ignored ({type(e).__name__})")

weather_wake = threading.Event()  # set when the location changes: fetch now

def weather_loop():
    while True:
        ok = fetch_weather()
        weather_wake.wait(max(5.0, num("weather_refresh_minutes", 60)) * 60 if ok else 600)  # failed: retry in 10 min
        weather_wake.clear()

def location():
    """For the page only (never logged, not in /api/v1)."""
    return {"name": str(config.get("location_name") or ("" if "latitude" in config else "Klagenfurt")),
            "latitude": num("latitude", 46.62), "longitude": num("longitude", 14.31)}

def geocode(q):
    """City search for Settings > Location. The query isn't logged."""
    q = str(q or "").strip()[:80]
    if len(q) < 2:
        return {"ok": False, "msg": "Type at least 2 letters", "results": []}
    url = GEOCODE_URL + "?" + urllib.parse.urlencode({"name": q, "count": 6, "language": "en", "format": "json"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tray-rfid"}), timeout=10) as r:
            found = json.load(r).get("results") or []
        res = [{"name": str(x["name"]), "region": ", ".join(str(x[k]) for k in ("admin1", "country") if x.get(k)),
                "latitude": float(x["latitude"]), "longitude": float(x["longitude"])} for x in found]
    except Exception as e:
        log(f"Location search failed ({type(e).__name__})")
        return {"ok": False, "msg": "Search failed (no internet?)", "results": []}
    return {"ok": True, "msg": "" if res else "No place found", "results": res}

def weather_stale():
    """Real time, not the simulated clock: the forecast was fetched in real time."""
    return weather["fetched_at"] is None or time.time() - weather["fetched_at"] > num("weather_stale_hours", 6) * 3600

def window_rows(lo, hi):
    """Hourly rows covering lo..hi -> (rows, note); note "off" | "unavailable" | None."""
    ov = weather_override()
    if ov == "off":
        return [], "off"
    if ov in SIM_WEATHER:
        h = lo - lo % 3600
        return [{"t": h + i * 3600, **SIM_WEATHER[ov]} for i in range(int((hi - h) // 3600) + 1)], None
    if weather_stale():
        return [], "unavailable"
    rows = [r for r in weather["hours"] if lo - 3600 < r["t"] <= hi]
    return (rows, None) if rows else ([], "unavailable")  # e.g. a simulated day past the forecast

def rule_match(groups, rows):
    """First OR group that holds in some hour -> (row, its clauses), at the hour where it is most extreme."""
    for g in groups:
        hits = [r for r in rows if all(r.get(v) is not None and OPS[op](r[v], x) for v, op, x in g)]
        if hits:
            v, op = g[0][:2]
            return (max if ">" in op else min)(hits, key=lambda r: r[v]), g
    return None

def weather_for(events, now):
    """Weather suggestions for a plan: {"items": [{name, reason, rule, tagged, on_tray}], "note", "window", "rows"}.
    Window = first start .. last end of the plan's events. Never an obligation: nothing here makes MISSING."""
    out = {"items": [], "note": None, "window": None, "rows": []}
    if not events:
        return out
    lo, hi = events[0]["start"], max(e.get("end", e["start"]) for e in events)
    rows, note = window_rows(lo, hi)
    out.update(note=note, window=(lo, hi), rows=rows)
    seen = {i.lower() for e in events for i in resolve(e)[0]}  # already on the plan: not again as weather
    on = {(n or "").lower() for _, n in tray_tags(now)}
    with lock:
        tagged = {n.lower() for n in data["tags"].values()}
        rs = list(rules)
    for rule in rs:
        try:
            hit = rule_match(parse_condition(rule["condition"]), rows)
        except ValueError:
            continue
        if not hit:
            continue
        r, g = hit
        reason = ", ".join(SAY[v].format(f"{round(r[v], 1):g}") for v, _, _ in g) + " at " + fmt(r["t"], False)
        for name in rule["items"]:
            if name.lower() not in seen:
                seen.add(name.lower())
                out["items"].append({"name": name, "reason": reason, "rule": rule["name"],
                                     "tagged": name.lower() in tagged, "on_tray": name.lower() in on})
    return out

def reminders(events, now):
    """Weather suggestions not on the tray right now: [{name, reason}]."""
    return [{"name": w["name"], "reason": w["reason"]} for w in weather_for(events, now)["items"] if not w["on_tray"]]

def reminder_text(items):
    """[{name, reason}] -> 'scarf, gloves (3 degrees at 08:00); umbrella (rain 70% at 08:00)'"""
    by = {}
    for w in items:
        by.setdefault(w["reason"], []).append(w["name"])
    return "; ".join(f'{", ".join(n)} ({r})' for r, n in by.items())

reminded = set()  # (day, items) already in a notification. ponytail: in memory, a restart may repeat one

def remember(events, day, now):
    """' Remember: umbrella (rain 70% at 08:00).' for weather items not on the tray, once per day per item set."""
    rem = reminders(events, now)
    key = (str(day), tuple(sorted(w["name"].lower() for w in rem)))
    if not rem or key in reminded:
        return ""
    reminded.add(key)
    return " Remember: " + reminder_text(rem) + "."

def weather_summary(events, now):
    """For /api/v1/plan: the hour now, min/max over the plan window, and why each weather item is there."""
    w = weather_for(events, now)
    cur = window_rows(now, now)[0]
    vals = lambda v: [r[v] for r in w["rows"] if r.get(v) is not None]
    by = {}
    for i in w["items"]:
        by.setdefault(i["reason"], []).append(i["name"])
    return {"simulated": None if weather_override() == "none" else weather_override(), "note": w["note"],
            "now": {v: cur[-1][v] for v in WEATHER_VARS} if cur else None,
            "window": w["window"] and {"from": iso(w["window"][0]), "to": iso(w["window"][1]),
                                       "min": {v: min(vals(v)) if vals(v) else None for v in WEATHER_VARS},
                                       "max": {v: max(vals(v)) if vals(v) else None for v in WEATHER_VARS}},
            "items": w["items"], "reasons": [f'{", ".join(n)}: {r}' for r, n in by.items()]}

def calendar_check(now):
    for ev in cal["events"]:
        if not remind_at(ev) <= now < ev["start"] or ev is not first_of_day(ev["start"]):
            continue  # one reminder a day, before the first event: later ones would come too late
        rest = rest_of_day(ev["start"])  # the whole day
        if not rest:
            continue
        key = f'{ev["uid"]}@{int(ev["start"])}'
        with lock:
            if key in data["notified"]:
                continue
            data["notified"] = {k: v for k, v in data["notified"].items() if v > now - 86400}
            data["notified"][key] = ev["start"]
            save()
        st = item_status(day_items(rest), now)
        missing = [i["name"] for i in st if i["status"] == "missing"]
        untagged = [i["name"] for i in st if i["status"] == "unknown"]
        rem = remember(rest, day_of(ev["start"]), now)
        if missing or untagged or rem:
            parts = ([f'missing from tray: {", ".join(missing)}'] if missing else []) + \
                    ([f'no tag: {", ".join(untagged)}'] if untagged else [])
            body = "; ".join(parts)
            notify(f"{label(rest)}." + (f" {body}" if body else "") + ("." if body and rem else "") + rem + stale_note(now))
        else:
            log(f"{label(rest)}: everything is on the tray")

def write_private(path, b):
    """Atomic write, owner-only (the calendar is personal): temp file, then rename over the old one."""
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(b)
    os.replace(tmp, path)

cached = {"text": None}  # ICS bytes last written to CACHE_ICS

def save_cache(text, now):
    if text != cached["text"]:  # the ICS only when it changed: spares the SD card a write a minute
        write_private(CACHE_ICS, text)
        cached["text"] = text
    write_private(CACHE_META, json.dumps({"fetched_at": now}).encode())

def drop_cache():
    cached["text"] = None
    for p in (CACHE_ICS, CACHE_META):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

def load_cache(now):
    """At startup: the last good calendar, so events show before (or without) the first fetch."""
    try:
        with open(CACHE_ICS, "rb") as f:
            text = f.read()
        with open(CACHE_META) as f:
            fetched = float(json.load(f)["fetched_at"])
        events = parse_ics(text, start_of_day(now), now + 7 * 86400)
    except FileNotFoundError:
        return
    except Exception as e:
        log(f"Calendar cache unreadable, ignored ({e})")
        return
    cached["text"] = text
    cal.update(events=events, updated=fetched)
    log(f"Calendar: cached copy from {fmt(fetched)} loaded")

def fetch_calendar(url, now):
    """One fetch. Only a good response replaces the events and the cache; on failure the old ones stay."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tray-rfid"})
        with urllib.request.urlopen(req, timeout=15) as r:  # HTTP errors raise here
            text = r.read()
    except Exception as e:
        return set_cal("unreachable", e, url)
    try:
        if not text.lstrip(b"\xef\xbb\xbf \t\r\n").upper().startswith(b"BEGIN:VCALENDAR"):
            raise ValueError("not an iCal file (empty or an error page)")
        events = parse_ics(text, start_of_day(now), now + 7 * 86400)  # from midnight: the page lists all of today
    except Exception as e:
        return set_cal("invalid", e, url)
    cal.update(events=events, updated=now)
    set_cal("ok")
    try:
        save_cache(text, now)
    except OSError as e:
        log(f"Calendar cache not saved ({e})")

def calendar_loop():
    while True:
        now = clock()
        url = config.get("ics_url", "")
        if not url:
            set_cal("unconfigured")
            cal["events"] = []
        elif now >= cal["next_fetch"]:
            cal["next_fetch"] = now + CAL_REFRESH
            fetch_calendar(url, now)
        try:
            calendar_check(clock())
        except Exception as e:
            log(f"Calendar check error: {e}")
        wake.wait(10)
        wake.clear()

# ---------- tray ----------

last_seen = {s: 0.0 for s in SLOTS}
last_uid = {s: None for s in SLOTS}
shown = set()  # uids on the tray in the previous loop, to log additions and removals
removed_at = {}  # uid -> when it left the tray, to spot the whole bag going at once

def seen(slot, now):
    """Read within TIMEOUT. A time in the future means the simulated clock was moved back (Settings > Demo):
    not seen, or the tag would stick on the tray until the clock caught up. A tag really there is read again at once."""
    return 0 <= now - last_seen[slot] <= TIMEOUT

def tray_tags(now):
    """Tags on the tray right now, read by any reader: [(uid, name or None)]."""
    with lock:
        found = {last_uid[s]: data["tags"].get(last_uid[s]) for s in SLOTS if seen(s, now)}
    return list(found.items())

def leaving_check(now, bag=None):
    """-> (problems, label of today's remaining events or None): checks everything still needed today.
    bag: names that were on the tray when it was lifted (default: what's on it now)."""
    rest = rest_of_day(now)
    if not rest:
        log("Leaving: no more events with items today.")
        return [], None
    st = item_status(day_items(rest), now, bag)
    problems = [f'missing {i["name"]}' for i in st if i["status"] == "missing"] + \
               [f'{i["name"]} has no tag' for i in st if i["status"] == "unknown"]
    if problems:
        notify(("Bag lifted without everything" if bag is not None else "Don't leave yet")
               + f" ({label(rest)}): " + "; ".join(problems) + stale_note(now))
    else:
        log(f"Leaving ({label(rest)}): all good.")
    return problems, label(rest)

evening = {"armed": None, "settle": 0, "changed": 0.0, "done": None}  # armed: "bag" | "first" | None

def evening_check(now):
    """In the evening: is tomorrow's plan on the tray? One notification if something is missing, else silent.
    -> (missing, message) or None when nothing is planned."""
    plan_for, day = focus_day(now)
    events = plan_events(day, now)
    if not events:
        log("Evening check: nothing to bring tomorrow")
        return None
    st = item_status(day_items(events), now)
    on = [i["name"] for i in st if i["status"] == "ok"]
    missing = [i["name"] for i in st if i["status"] == "missing"] + \
              [f'{i["name"]} (no tag)' for i in st if i["status"] == "unknown"]
    rem = remember(events, day, now)
    msg = ("Tomorrow: " + ", ".join(f'{e["title"]} {fmt(e["start"], False)}' for e in events)
           + f'. On the tray: {", ".join(on) or "nothing"}.' + (f' Missing: {", ".join(missing)}.' if missing else "") + rem)
    if missing or rem:
        notify(msg + stale_note(now))
    else:
        log("Evening check: tomorrow ready")
    return missing, msg

def evening_tick(current, added, now):
    """Arms the evening check (bag placed, or the evening's first tag) and fires it once the tray is settled."""
    plan_for, day = focus_day(now)
    with lock:
        bag = data.get("bag")
    if plan_for != "tomorrow":
        evening["armed"] = None
    elif bag and bag in added:
        evening.update(armed="bag", settle=BAG_SETTLE)
    elif not bag and added and evening["done"] != day:
        evening.update(armed="first", settle=EVENING_SETTLE)
    if evening["armed"] and now - evening["changed"] >= evening["settle"]:
        still_there = bag in current if evening["armed"] == "bag" else bool(current)
        evening["armed"] = None
        if still_there:  # taken off again before settling: no check, the next placement re-arms it
            evening["done"] = day
            threading.Thread(target=evening_check, args=(now,), daemon=True).start()

def tray_changed(current, now):
    """Logs additions/removals; if everything left the tray at once, the bag was lifted: check it.
    In the evening a bag put down (or the first thing placed) checks tomorrow's plan instead."""
    added = current.keys() - shown
    if added or shown - current.keys():
        evening["changed"] = now
    for uid in added:
        log(f"On tray: {current[uid] or 'unnamed tag ' + uid}")
    for uid in shown - current.keys():
        removed_at[uid] = now
        with lock:
            log(f"Removed: {data['tags'].get(uid) or 'tag ' + uid}")
    went_empty = shown and not current
    shown.clear()
    shown.update(current)
    evening_tick(current, added, now)
    for uid in [u for u, t in removed_at.items() if not 0 <= now - t <= LIFT_WINDOW]:
        del removed_at[uid]
    if went_empty and len(removed_at) >= LIFT_MIN:
        with lock:
            bag = {(data["tags"].get(u) or "").lower() for u in removed_at} - {""}
        removed_at.clear()
        if focus_day(now)[0] == "tomorrow" and not any(e["start"] > now for e in rest_of_day(now)):
            return log("Bag lifted (evening, nothing left to start today: no check)")
        log("Bag lifted")
        # off the reader loop: notify() can take seconds; only notifies if something is missing
        threading.Thread(target=leaving_check, args=(now, bag), daemon=True).start()

# ---------- overall status (LEDs + /api/v1) ----------

def iso(t):
    return datetime.fromtimestamp(t, TZ).isoformat(timespec="seconds")

def cal_stale(now):
    """Calendar data older than stale_after_hours, or none at all once a fetch has failed."""
    if not config.get("ics_url") or cal["updated"] is None and cal["state"] == "loading":
        return False
    return now - (cal["updated"] or 0) > stale_after()

def ago(seconds):
    return f"{seconds / 60:.0f} min" if seconds < 3600 else f"{seconds / 3600:.1f} h"

def stale_note(now):
    """Appended to calendar-based notifications while the data is stale."""
    return f" (calendar data {ago(now - cal['updated'])} old)" if cal_stale(now) and cal["updated"] else ""

def status(now):
    """MISSING > STALE > IDLE (nothing to bring) > READY, checked on the plan in focus (today or tomorrow).
    Stale data still shows MISSING: what it does know is worth acting on."""
    plan_for, rest = focus_plan(now)
    st = item_status(day_items(rest), now)
    missing = [i["name"] for i in st if i["status"] == "missing"]
    no_tag = [i["name"] for i in st if i["status"] == "unknown"]
    nxt = rest[0] if rest else None
    w = weather_for(rest, now)
    return {"state": "MISSING" if missing or no_tag else "STALE" if cal_stale(now) else
                     "IDLE" if not rest else "READY",
            "plan_for": plan_for, "missing": missing, "no_tag": no_tag,
            # weather suggestions not on the tray: shown, never MISSING
            "reminders": [{"name": i["name"], "reason": i["reason"]} for i in w["items"] if not i["on_tray"]],
            "weather_simulated": None if weather_override() == "none" else weather_override(),
            "weather_unavailable": w["note"] == "unavailable",
            "next_event": nxt and {"title": nxt["title"], "start": iso(nxt["start"]),
                                   "end": iso(nxt.get("end", nxt["start"]))},
            "time": iso(now)}

def v1_items(now):
    rest = focus_plan(now)[1]
    with lock:
        tags = sorted(data["tags"].values(), key=str.lower)
        slot = {data["tags"][last_uid[s]].lower(): s for s in SLOTS
                if seen(s, now) and last_uid[s] in data["tags"]}
    expected = day_items(rest)
    tagged = {n.lower() for n in tags}
    return {"items": [{"name": n, "expected": n.lower() in {e.lower() for e in expected},
                       "on_tray": n.lower() in slot, "slot": slot.get(n.lower()), "tagged": n.lower() in tagged}
                      for n in dedupe(tags + expected)], "time": iso(now)}

def item_reasons(ev):
    """Each item of an event with why it's there: 'override', 'preset:<name>', 'notes' (Bring: line)."""
    items, override, names = resolve(ev)
    if override:
        return [{"name": i, "reasons": ["override"]} for i in items]
    with lock:
        lists = {n: {i.lower() for i in data["presets"].get(n, {}).get("items", [])} for n in names}
    bring = {b.lower() for b in ev["bring"]}
    return [{"name": i, "reasons": [f"preset:{n}" for n in names if i.lower() in lists[n]]
             + (["notes"] if i.lower() in bring else [])} for i in items]

def v1_plan(now):
    rest = rest_of_day(now)
    word = {"ok": "on_tray", "missing": "missing", "unknown": "no_tag"}
    return {"date": day_of(now).isoformat(),
            "events": [{"title": e["title"], "start": iso(e["start"]), "end": iso(e.get("end", e["start"])),
                        "done": max(e["start"], e.get("end", 0)) < now, "items": item_reasons(e)}
                       for e in cal["events"] if day_of(e["start"]) == day_of(now)],
            "required": [{"name": i["name"], "status": word[i["status"]]}
                         for i in item_status(day_items(rest), now)],
            "reminders": reminders(rest, now),
            "weather": weather_summary(rest, now),
            "time": iso(now)}

def v1_health(now):
    up = cal["updated"]
    return {"calendar": {"state": cal["state"], "last_updated": up and iso(up),
                         "age_minutes": None if up is None else round((now - up) / 60, 1),
                         "stale": cal_stale(now)},
            "weather": {"fetched_at": weather["fetched_at"] and iso(weather["fetched_at"]),  # no location, no values
                        "age_minutes": None if weather["fetched_at"] is None else round((time.time() - weather["fetched_at"]) / 60, 1),
                        "stale": weather_stale()},
            "time": iso(now)}

V1 = {"/api/v1/status": status, "/api/v1/items": v1_items, "/api/v1/plan": v1_plan, "/api/v1/health": v1_health}

PROTECTED = {"/api/v1/plan", "/api/v1/items", "/api/v1/stream"}  # these show calendar titles

def session_value():
    """Cookie for pages the tray serves itself: derived from the token, so the cookie is not the token."""
    return hmac.new(config.get("api_token", "").encode(), b"tray display session", "sha256").hexdigest()

def on_lan(addr):
    ip = ipaddress.ip_address(addr)
    return ip.is_private or ip in TAILNET

current = {"status": None, "version": 0}  # version 0 = not computed yet
changed = threading.Condition()

def status_loop():
    """Once a second: recompute the status; on a change drive the LEDs and wake /api/v1/stream clients."""
    while True:
        try:
            s = status(clock())
            if current["status"] is None or {**s, "time": 0} != {**current["status"], "time": 0}:
                led.set_state(s["state"], log)
                log_event("state", state=s["state"], detail=f'plan_for={s["plan_for"]} missing={"|".join(s["missing"])} '
                                                            f'no_tag={"|".join(s["no_tag"])}')
                with changed:
                    current.update(status=s, version=current["version"] + 1)
                    changed.notify_all()
        except Exception as e:
            log(f"Status error: {e}")
        time.sleep(1)

# ---------- web ----------

def state(now):
    with lock:
        tags = dict(data["tags"])
        un = dict(data["unnamed"])
    upcoming = [e for e in cal["events"] if e["start"] > now]
    today = rest_of_day(now)
    first = first_of_day(now)
    plan_for, focus = focus_plan(now)
    tomorrow = focus if plan_for == "tomorrow" else []
    events = [event_view(e, now) for e in upcoming[:max(5, len(today) + len(tomorrow))]]
    return {"on_tray": [{"uid": u, "name": n} for u, n in tray_tags(now)], "names": sorted(set(tags.values())),
            "tags": [{"uid": u, "name": n} for u, n in sorted(tags.items(), key=lambda kv: kv[1])],
            "unnamed": [{"uid": u, **i} for u, i in sorted(un.items(), key=lambda kv: kv[1]["seen"])],
            "today": {"keys": [event_key(e) for e in today], "items": item_status(day_items(today), now),
                      "check": fmt(remind_at(first), False) if remind_at(first) > now else None}  # None: already sent
                     if today else None,
            "day": [event_view(e, now) for e in cal["events"] if day_of(e["start"]) == day_of(now)],
            "events": events, "next": events[0] if events else None, "log": list(reversed(logbuf)),
            "cal": {"state": cal["state"], "updated": cal["updated"] and fmt(cal["updated"], False)},
            "calendar_updated": cal["updated"] and iso(cal["updated"]),
            "age_minutes": None if cal["updated"] is None else int((now - cal["updated"]) / 60),
            "stale": cal_stale(now),
            "ntfy": masked(config.get("ntfy_topic", "")), "ics": masked(config.get("ics_url", "")),
            "lead": lead_minutes(),
            "plan_for": plan_for,  # "tomorrow" between evening_from and evening_until
            "focus_date": focus_day(now)[1].isoformat(),
            "tomorrow": {"keys": [event_key(e) for e in tomorrow], "items": item_status(day_items(tomorrow), now)}
                        if plan_for == "tomorrow" else None,
            "bag": data.get("bag"), "now": now, "simulated": fake["at"] or None,
            "weather": weather_state(today, tomorrow if plan_for == "tomorrow" else None, now)}

def weather_state(today, tomorrow, now):
    """For the page: weather items with reasons for today's (and in the evening tomorrow's) plan."""
    t, m = weather_for(today, now), tomorrow is not None and weather_for(tomorrow, now)
    ov = weather_override()
    f, cur = m or t, window_rows(now, now)[0]
    vals = lambda v: [r[v] for r in f["rows"] if r.get(v) is not None]
    return {"override": ov, "simulated": None if ov == "none" else ov,
            "note": (m or t)["note"], "today": t["items"], "tomorrow": m["items"] if m else None,
            "location": location(),
            "now": {v: cur[-1][v] for v in WEATHER_VARS} if cur else None,
            "age_minutes": None if weather["fetched_at"] is None else int((time.time() - weather["fetched_at"]) / 60),
            "range": f["window"] and f["rows"] and {   # over the plan in focus
                "from": fmt(f["window"][0], False), "to": fmt(f["window"][1], False),
                **{f"{k}_{v}": agg(vals(v)) if vals(v) else None for v in WEATHER_VARS for k, agg in (("min", min), ("max", max))}}}

def event_view(ev, now):
    items, override, names = resolve(ev)
    words = re.findall(r"\w{3,}", ev["title"].lower())
    return {"key": event_key(ev), "title": ev["title"], "start_ts": int(ev["start"]), "end_ts": int(ev.get("end", ev["start"])), "when": fmt(ev["start"]),
            "check": fmt(remind_at(ev), False),
            "items": item_status(items, now), "override": override, "presets": names, "bring": ev["bring"],
            "keyword": names[0] if names else (words[0] if words else "")}

def event_items(b):
    key, scope, items = str(b.get("key", "")), b.get("scope"), b.get("items", [])
    ev = next((e for e in cal["events"] if event_key(e) == key), None)
    if not ev:
        return {"ok": False, "msg": "Event not found"}
    if not isinstance(items, list) or len(items) > 30:
        return {"ok": False, "msg": "Invalid list"}
    items = dedupe(items)
    with lock:
        if scope == "event":
            yesterday = datetime.fromtimestamp(clock() - 86400, TZ).date().isoformat()
            data["event_overrides"] = {k: v for k, v in data["event_overrides"].items()
                                       if k.rsplit("|", 1)[-1] >= yesterday}
            data["event_overrides"][key] = items
            msg = "List saved for this event only"
        elif scope == "reset":
            data["event_overrides"].pop(key, None)
            msg = "Automatic list restored"
        elif scope == "preset":
            word = str(b.get("preset", "")).strip()[:30]
            name = find_preset(word)
            if name and name not in matching_presets(ev["title"]):
                return {"ok": False, "msg": f"The list “{name}” doesn't apply to this event"}
            if not name:
                if not word_in(word, ev["title"]):
                    return {"ok": False, "msg": f"“{word}” is not in the event title"}
                name = word.lower()
                data["presets"][name] = {"aliases": [], "items": []}
            bring_l = {p.lower() for p in ev["bring"]}  # the 'Bring:' line stays in the notes
            data["presets"][name]["items"] = [i for i in items if i.lower() not in bring_l]
            data["event_overrides"].pop(key, None)
            msg = f"List “{name}” updated"
        else:
            return {"ok": False, "msg": "Invalid choice"}
        save()
    log(f'{ev["title"]}: {msg}')
    return {"ok": True, "msg": msg}

def list_presets():
    with lock:
        return {"presets": [{"name": n, "aliases": p.get("aliases", []), "items": p.get("items", [])}
                            for n, p in data["presets"].items()]}

def save_preset(b):
    name, old = str(b.get("name", "")).strip()[:30], b.get("old_name") or None
    aliases, items = b.get("aliases", []), b.get("items", [])
    if isinstance(aliases, str):
        aliases = aliases.split(",")
    if not name or not isinstance(aliases, list) or not isinstance(items, list) \
            or len(aliases) > 10 or len(items) > 30:
        return {"ok": False, "msg": "Invalid data"}
    aliases = [a for a in dedupe(aliases) if a.lower() != name.lower()]
    with lock:
        if old is not None and old not in data["presets"]:
            return {"ok": False, "msg": "List not found"}
        for w in [name, *aliases]:
            other = find_preset(w)
            if other and other != old:
                return {"ok": False, "msg": f"“{w}” is already used by the list “{other}”"}
        if old is not None and old != name:
            del data["presets"][old]
        data["presets"][name] = {"aliases": aliases, "items": dedupe(items)}
        save()
    log(f"List “{name}” saved" + (f" (was “{old}”)" if old and old != name else ""))
    return {"ok": True, "msg": "List saved"}

def delete_preset(b):
    name = str(b.get("name", ""))
    with lock:
        if name not in data["presets"]:
            return {"ok": False, "msg": "List not found"}
        del data["presets"][name]
        save()
    log(f"List “{name}” deleted")
    return {"ok": True, "msg": "List deleted"}

def settings(b):
    topic = str(b.get("ntfy_topic", "")).strip()
    url = str(b.get("ics_url", "")).strip()
    lead = str(b.get("lead_minutes", "")).strip()
    wo = str(b.get("weather_override") or "").strip().lower()
    if wo and wo not in OVERRIDES:
        return {"ok": False, "msg": "Invalid simulated weather"}
    if topic.startswith(("http://", "https://")):
        topic = topic.rstrip("/").rsplit("/", 1)[-1]  # pasted the ntfy.sh/topic link
    if topic and not re.fullmatch(r"[\w-]{1,64}", topic):
        return {"ok": False, "msg": "Invalid ntfy topic (letters, digits, - and _)"}
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if url and not re.match(r"https?://\S+$", url, re.I):
        return {"ok": False, "msg": "Invalid calendar URL"}
    if lead and not (lead.isdigit() and int(lead) <= 240):
        return {"ok": False, "msg": "Invalid lead time (0-240 minutes)"}
    loc = None
    if b.get("latitude") not in (None, "") or b.get("longitude") not in (None, ""):
        try:
            lat, lon = float(b.get("latitude")), float(b.get("longitude"))
            assert -90 <= lat <= 90 and -180 <= lon <= 180
        except (TypeError, ValueError, AssertionError):
            return {"ok": False, "msg": "Invalid coordinates (latitude -90..90, longitude -180..180)"}
        loc = (round(lat, 4), round(lon, 4), re.sub(r"[<>]", "", str(b.get("location_name") or "")).strip()[:80])
    if not (topic or url or lead or wo or loc):
        return {"ok": False, "msg": "Nothing to save"}
    with lock:
        if topic:
            config["ntfy_topic"] = topic
        if url:
            config["ics_url"] = url
            cal.update(state="loading", events=[], updated=None, next_fetch=0)
            drop_cache()  # it belongs to the old calendar
        if lead:
            config["lead_minutes"] = int(lead)
        if wo:
            config["weather_override"] = wo
        if loc:
            config.update(latitude=loc[0], longitude=loc[1], location_name=loc[2])
            weather.update(hours=[], fetched_at=None)  # the old forecast is for the old place
        save_config()
    wake.set()
    if loc:
        weather_wake.set()
    log("Settings updated: " + ", ".join(
        n for n, v in (("ntfy topic", topic), ("calendar", url), ("lead time", lead),
                      (f"simulated weather {wo}", wo), ("location", loc)) if v))  # no place, no coordinates
    return {"ok": True, "msg": "Saved"}

def demo_time(b):
    """Demo: simulated clock from the page, at once and in memory only (a restart goes back to real time)."""
    v = str(b.get("fake_now") or "").strip()[:40]
    try:
        set_fake_now(v)
    except ValueError:
        return {"ok": False, "msg": "Invalid date and time"}
    evening.update(armed=None, changed=0.0, done=None)  # a new evening starts from scratch
    removed_at.clear()  # removal times from the old clock would look like a bag lift
    cal["next_fetch"] = 0  # reload the calendar around the new time
    wake.set()
    log(f"SIMULATED TIME: clock set to {v}" if v else "Back to the real clock")
    log_event("clock", detail=f"simulated from {v}" if v else "real clock")
    return {"ok": True, "msg": "Simulated time set" if v else "Back to real time"}

def api(path, b):
    name = str(b.get("name", "")).strip()[:30]
    uid = str(b.get("uid", ""))
    if path == "/api/leave":
        problems, title = leaving_check(clock())
        return {"problems": problems, "event": title}
    if path == "/api/settings":
        return settings(b)
    if path == "/api/event_items":
        return event_items(b)
    if path == "/api/presets":
        return save_preset(b)
    if path == "/api/rules":
        return save_rules(b)
    if path == "/api/demo_time":
        return demo_time(b)
    if path == "/api/test_notify":
        if not config.get("ntfy_topic"):
            return {"ok": False, "msg": "ntfy topic not configured"}
        ok = notify("Test notification from the tray")
        return {"ok": ok, "msg": "Notification sent" if ok else "Sending failed, see Activity"}
    with lock:
        if path == "/api/name":
            if name and (uid in data["unnamed"] or uid in data["tags"]):
                old = data["tags"].get(uid)
                data["unnamed"].pop(uid, None)
                data["tags"][uid] = name
                save()
                log(f"Tag {uid}: {old + ' -> ' if old else ''}{name}")
        elif path == "/api/bag":  # uid "" = no bag tag
            if uid and uid not in data["tags"]:
                return {"ok": False, "msg": "Unknown tag"}
            if uid:
                data["bag"] = uid
            else:
                data.pop("bag", None)
            save()
            log(f"Bag tag: {data['tags'][uid]}" if uid else "Bag tag cleared")
        elif path == "/api/delete":
            if uid in data["tags"] or uid in data["unnamed"]:
                old = data["tags"].pop(uid, None)
                data["unnamed"].pop(uid, None)
                if data.get("bag") == uid:
                    del data["bag"]
                save()
                log(f"Tag {uid} deleted" + (f" ({old})" if old else ""))
        else:
            return None
    return {"ok": True}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authorized(self):
        if not PIN:
            return True
        h = self.headers.get("Authorization", "")
        try:
            pw = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)[1]
        except Exception:
            return False
        return h.startswith("Basic ") and hmac.compare_digest(pw.encode(), PIN.encode())

    def reply(self, code, body, ctype="application/json", cors=False, headers=()):
        out = (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        if code == 401 and ctype != "application/json":
            self.send_header("WWW-Authenticate", 'Basic realm="Tray"')
        self.end_headers()
        self.wfile.write(out)

    def token_ok(self, cookie=False):
        """Authorization: Bearer <api_token>; with cookie=True also the session cookie set by /display."""
        token = config.get("api_token", "")
        if not token:
            return False
        if hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()):
            return True
        if not cookie:
            return False
        try:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", "")).get("tray_session")
        except http.cookies.CookieError:
            return False
        return c is not None and hmac.compare_digest(c.value.encode(), session_value().encode())

    def v1_get(self, path):
        """Local network (or Tailscale) only, CORS on. status/health open, PROTECTED need the token."""
        if not on_lan(self.client_address[0]):
            return self.reply(403, {"error": "local network only"}, cors=True)
        if path in PROTECTED and not self.token_ok(cookie=path == "/api/v1/stream"):
            return self.reply(401, {"error": "Bearer token required"}, cors=True)
        if path == "/api/v1/stream":
            return self.stream()
        f = V1.get(path)
        self.reply(200 if f else 404, f(clock()) if f else {"error": "not found"}, cors=True)

    def stream(self):
        """Server-Sent Events: the status now, then again on every change; a ping every 15 s keeps it open."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        seen = 0
        try:
            while True:
                with changed:
                    changed.wait_for(lambda: current["version"] != seen, timeout=15)
                    version, s = current["version"], current["status"]
                if version != seen:
                    seen = version
                    self.wfile.write(f"data: {json.dumps(s)}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass  # client went away

    def v1_write(self):
        """No /api/v1 endpoint changes data yet; any that does must pass this Bearer check first."""
        if not self.token_ok():
            return self.reply(401, {"error": "Bearer token required"})
        self.reply(405, {"error": "read-only"})

    def do_OPTIONS(self):
        """CORS preflight: lets a page on another device send the Authorization header."""
        if not self.path.startswith("/api/v1/"):
            return self.reply(404, "not found", "text/plain")
        self.send_response(204)
        for k, v in (("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Methods", "GET"),
                     ("Access-Control-Allow-Headers", "Authorization"), ("Access-Control-Max-Age", "600")):
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/v1/"):
            return self.v1_get(path)
        if not self.authorized():
            return self.reply(401, "PIN required", "text/plain")
        if path == "/api/state":
            return self.reply(200, state(clock()))
        if path == "/api/presets":
            return self.reply(200, list_presets())
        if path == "/api/geocode":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").get("q", [""])[0]
            return self.reply(200, geocode(q))
        if path == "/api/rules":
            with lock:
                return self.reply(200, {"rules": list(rules), "vars": WEATHER_VARS, "override": weather_override()})
        if path == "/":
            with open(UI_FILE, encoding="utf-8") as f:
                return self.reply(200, f.read(), "text/html")
        if path == "/display":  # LAN only: hands the page a same-origin session for /api/v1/stream
            if not on_lan(self.client_address[0]):
                return self.reply(403, "local network only", "text/plain")
            with open(DISPLAY_FILE, encoding="utf-8") as f:
                return self.reply(200, f.read(), "text/html", headers=[("Set-Cookie",
                    f"tray_session={session_value()}; Path=/api/v1/stream; HttpOnly; SameSite=Strict; Max-Age=31536000")])
        self.reply(404, "not found", "text/plain")

    def body(self):
        if not self.authorized():
            return self.reply(401, "PIN required", "text/plain")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.reply(415, {"error": "json"})  # blocks cross-site form posts
        h = self.headers.get("Content-Length", "0").strip()
        length = int(h) if h.isdigit() else -1  # negative / garbage would make read() wait for the connection to close
        if not 0 <= length <= 8000:
            return self.reply(413, {"error": "body too large or bad length"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            assert isinstance(body, dict)
            return body
        except Exception:
            return self.reply(400, {"error": "json"})

    def do_POST(self):
        if self.path.startswith("/api/v1/"):
            return self.v1_write()
        body = self.body()
        if body is not None:
            res = api(self.path, body)
            self.reply(200 if res else 404, res or {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/v1/"):
            return self.v1_write()
        body = self.body()
        if body is not None:
            res = delete_preset(body) if self.path == "/api/presets" else None
            self.reply(200 if res else 404, res or {"error": "not found"})

# ---------- main ----------

def main():
    import RPi.GPIO as GPIO
    from mfrc522 import MFRC522

    readers = {
        "R1": MFRC522(bus=0, device=0, pin_mode=GPIO.BCM, pin_rst=25),
        "R2": MFRC522(bus=0, device=1, pin_mode=GPIO.BCM, pin_rst=25),
        "R3": MFRC522(bus=1, device=0, pin_mode=GPIO.BCM, pin_rst=25),
    }

    def read_uid(r):
        status, _ = r.MFRC522_Request(r.PICC_REQIDL)
        if status != r.MI_OK:
            return None
        status, uid = r.MFRC522_Anticoll()
        if status != r.MI_OK:
            return None
        return ":".join(f"{b:02X}" for b in uid[:4])

    if not os.path.exists(CONFIG_FILE):
        save_config()
        log(f"Created {CONFIG_FILE} (chmod 600): set topic and calendar from the web page")
    if not config.get("api_token"):
        config["api_token"] = secrets.token_urlsafe(32)
        save_config()
        log(f"API token created in {CONFIG_FILE} (api_token)")
    global events_q
    events_q = queue.Queue()
    threading.Thread(target=events_writer, daemon=True).start()
    log_event("start", detail=f"simulated clock from {fake['at']}" if fake["at"] else "real clock")
    if config.get("ics_url"):
        load_cache(clock())
    if not os.path.exists(RULES_FILE):
        save_rules({"rules": rules})  # the defaults, so the file is there to read
    load_weather_cache()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=calendar_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    base = BASE_URL or f"http://{local_ip()}:{PORT}"
    use_stdin = sys.stdin is not None and sys.stdin.isatty()  # service: no terminal, no Enter prompt
    log(f"Tray running. Page: {base}" + (" - Enter = 'leaving', Ctrl+C = quit" if use_stdin else ""))
    log("Notifications " + ("configured" if config.get("ntfy_topic") else "not configured (log only)"))
    if fake["at"]:
        log(f"SIMULATED TIME: clock started at {fake['at']}")
    if weather_override() != "none":
        log(f"SIMULATED WEATHER: {weather_override()}")

    slot_was = {s: None for s in SLOTS}
    try:
        while True:
            now = clock()
            for slot, r in readers.items():
                uid = read_uid(r)
                if not uid:
                    continue
                last_seen[slot] = now
                last_uid[slot] = uid
                new = False
                with lock:
                    if uid not in data["tags"] and uid not in data["unnamed"]:
                        data["unnamed"][uid] = {"slot": slot, "seen": now_str("%d/%m %H:%M")}
                        save()
                        new = True
                if new:
                    notify(f"New tag detected (UID {uid}). Give it a name when you like.",
                           click=base + "/")
            for slot in SLOTS:  # slot changes for events.csv (a removal shows once the tag timed out)
                uid = last_uid[slot] if seen(slot, now) else None
                if uid != slot_was[slot]:
                    gone = slot_was[slot]
                    slot_was[slot] = uid
                    with lock:
                        name = data["tags"].get(uid or gone) or f"unnamed:{uid or gone}"
                    log_event("slot", slot, name, "", f"placed {uid}" if uid else f"removed {gone}")
            tray_changed(dict(tray_tags(now)), now)
            if use_stdin and select.select([sys.stdin], [], [], 0)[0]:
                if sys.stdin.readline():
                    leaving_check(clock())
                else:
                    use_stdin = False  # stdin closed (e.g. systemd/nohup)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:  # also on a crash: put the LEDs back to the Pi's defaults
        led.restore()
        GPIO.cleanup()

if __name__ == "__main__":
    main()
