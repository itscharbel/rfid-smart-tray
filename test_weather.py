# Weather rules with mocked Open-Meteo responses (no network, no Pi hardware). Run: python3 test_weather.py
import os, json, stat, time
# Never read the real tray_config.json (secrets): point tray4 at an empty temp folder before importing it.
import tempfile as _tf
os.environ["TRAY_CONFIG"] = os.path.join(_tf.mkdtemp(), "tray_config.json")
os.environ.pop("TRAY_FAKE_NOW", None)
import tray4
if tray4.CONFIG_FILE == "/home/pi/tray_config.json":
    raise SystemExit("refusing to run: tests must not use the real tray_config.json")
TMP = os.path.dirname(tray4.CONFIG_FILE)
assert os.path.dirname(tray4.RULES_FILE) == TMP and os.path.dirname(tray4.WEATHER_CACHE) == TMP  # never the real files
from datetime import datetime
from zoneinfo import ZoneInfo

def _no_network(*a, **k):
    raise OSError("network blocked in tests")
tray4.urllib.request.urlopen = _no_network
sent, logged = [], []
tray4.notify = lambda msg, click=None: sent.append(msg) or True
tray4.save = lambda: None
tray4.log = logged.append

VIE = ZoneInfo("Europe/Vienna")
t = lambda *a: datetime(*a, tzinfo=VIE).timestamp()
tray4.config.clear()
tray4.config.update(lead_minutes=15, ics_url="https://example.invalid/x.ics", latitude=12.3456, longitude=65.4321)
tray4.rules[:] = [dict(r) for r in tray4.DEFAULT_RULES]
tray4.data.update(tags={"L": "laptop", "O": "bottle", "K": "keys"}, unnamed={}, notified={}, event_overrides={},
                  presets={"work": {"aliases": [], "items": ["laptop", "bottle"]}})
tray4.data.pop("bag", None)
CALM = {"temperature_2m": 12, "apparent_temperature": 11, "precipitation_probability": 10, "precipitation": 0,
        "cloud_cover": 80, "uv_index": 1}

def forecast(**hours):
    """Open-Meteo-shaped JSON for 12-14 Nov 2026, calm weather except hours given as h08={...}."""
    times = [f"2026-11-{d}T{h:02}:00" for d in (12, 13, 14) for h in range(24)]
    hourly = {"time": times}
    for v in tray4.WEATHER_VARS:
        hourly[v] = [hours.get(f"h{s[11:13]}", {}).get(v, CALM[v]) if s.startswith("2026-11-12") else CALM[v] for s in times]
    return {"latitude": 12.3456, "longitude": 65.4321, "hourly": hourly}

class Resp:
    def __init__(self, body): self.body = body
    def read(self, *a): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): pass

asked = []
def serve(doc):
    """urlopen that answers like Open-Meteo (doc: dict, or bytes for a broken reply) and records the request."""
    def urlopen(req, timeout=None):
        asked.append((req.full_url, timeout))
        return Resp(doc if isinstance(doc, bytes) else json.dumps(doc).encode())
    tray4.urllib.request.urlopen = urlopen

def load(**hours):
    serve(forecast(**hours))
    assert tray4.fetch_weather()
    tray4.urllib.request.urlopen = _no_network
    tray4.reminded.clear()

def ev(uid, title, h0, h1, bring=()):
    return {"uid": uid, "title": title, "start": t(2026, 11, 12, h0), "end": t(2026, 11, 12, h1), "bring": list(bring), "travel": 0}

WORK = ev("w", "Work", 8, 12)
def items(events=(WORK,)):
    return [(w["name"], w["reason"]) for w in tray4.weather_for(list(events), t(2026, 11, 12, 7))["items"]]

# ---------- fetch: request, cache, no coordinates in logs or cache ----------
print("fetch")
load()
url, timeout = asked[-1]
for p in ("hourly=temperature_2m%2Capparent_temperature%2Cprecipitation_probability%2Cprecipitation%2Ccloud_cover%2Cuv_index",
          "timezone=Europe%2FVienna", "forecast_days=3"):
    assert p in url, p
assert url.startswith(tray4.WEATHER_URL + "?") and timeout == 10
assert stat.S_IMODE(os.stat(tray4.WEATHER_CACHE).st_mode) == 0o600
cache = open(tray4.WEATHER_CACHE).read()
assert "12.3456" not in cache and "65.4321" not in cache and "fetched_at" in cache
assert len(tray4.weather["hours"]) == 72 and not tray4.weather_stale()

# ---------- each rule and its boundary values ----------
print("rules and boundaries")
assert items() == []                                                           # calm: nothing
load(h09={"precipitation_probability": 50}); assert items() == [("umbrella", "rain 50% at 09:00")]
load(h09={"precipitation_probability": 49}); assert items() == []
load(h10={"precipitation": 0.5}); assert items() == [("umbrella", "rain 0.5 mm at 10:00")]
load(h10={"precipitation": 0.4}); assert items() == []
load(h09={"precipitation_probability": 60}, h10={"precipitation_probability": 70})
assert items() == [("umbrella", "rain 70% at 10:00")]                         # the worst hour is the reason
load(h11={"apparent_temperature": 28, "cloud_cover": 50}); assert items() == [("sunglasses", "feels like 28 degrees, clouds 50% at 11:00")]
load(h11={"apparent_temperature": 27.9, "cloud_cover": 50}); assert items() == []
load(h11={"apparent_temperature": 30, "cloud_cover": 51}); assert items() == []
load(h10={"apparent_temperature": 30}, h11={"cloud_cover": 20}); assert items() == []   # both in the same hour
load(h12={"uv_index": 6}); assert items() == [("sunscreen", "UV 6 at 12:00")]
load(h12={"uv_index": 5.9}); assert items() == []
load(h08={"temperature_2m": 5}); assert items() == [("scarf", "5 degrees at 08:00"), ("gloves", "5 degrees at 08:00")]
load(h08={"temperature_2m": 5.1}); assert items() == []
load(h08={"temperature_2m": 3}, h09={"temperature_2m": 4}); assert items()[0] == ("scarf", "3 degrees at 08:00")

# ---------- the plan window: first start .. last end ----------
print("plan window")
load(h07={"precipitation_probability": 90}); assert items() == []             # before Work 08:00
load(h13={"precipitation_probability": 90}); assert items() == []             # after Work ends at 12:00
load(h14={"precipitation_probability": 90})
assert items([ev("a", "Work", 8, 9), ev("b", "Gym", 17, 18)]) == [("umbrella", "rain 90% at 14:00")]  # between two events
assert items([]) == [] and tray4.weather_for([], t(2026, 11, 12, 7))["window"] is None           # no events: no weather rules

# ---------- Porta: adds to the lists and keeps the weather ----------
print("Porta: line")
load(h09={"precipitation_probability": 90})
dinner = ev("d", "Work dinner", 8, 12, bring=["keys"])
assert tray4.resolve(dinner) == (["laptop", "bottle", "keys"], False, ["work"])  # work list + the notes line
assert items([dinner]) == [("umbrella", "rain 90% at 09:00")]                 # weather still suggested
assert items([ev("x", "Walk", 8, 12, bring=["umbrella"]), WORK]) == []        # already on the plan: not twice

# ---------- weather is only a suggestion, tagged or not ----------
print("suggestion only")
tray4.cal.update(state="ok", updated=t(2026, 11, 12, 6), events=[WORK])
NOW = t(2026, 11, 12, 7, 0)
def on_tray(*uids, now=None):
    for s in tray4.SLOTS:
        tray4.last_seen[s], tray4.last_uid[s] = 0.0, None
    for s, u in zip(tray4.SLOTS, uids):
        tray4.last_seen[s], tray4.last_uid[s] = now or NOW, u
on_tray("L", "O")
load(h09={"precipitation_probability": 70})
s = tray4.status(NOW)
assert s["state"] == "READY" and s["missing"] == [] and s["no_tag"] == [], s           # a reminder never blocks READY
assert s["reminders"] == [{"name": "umbrella", "reason": "rain 70% at 09:00"}]
tray4.data["tags"]["U"] = "umbrella"                                                    # a tag changes nothing...
s = tray4.status(NOW)
assert s["state"] == "READY" and s["missing"] == [] and s["reminders"] == [{"name": "umbrella", "reason": "rain 70% at 09:00"}], s
assert "umbrella" not in tray4.day_items([WORK])
on_tray("L", "O", "U")                                                                  # ...except: on the tray, no reminder
s = tray4.status(NOW)
assert s["state"] == "READY" and s["reminders"] == [], s
assert tray4.state(NOW)["weather"]["today"][0]["on_tray"] and tray4.state(NOW)["weather"]["today"][0]["tagged"]
assert tray4.v1_plan(NOW)["reminders"] == [] and tray4.v1_plan(NOW)["weather"]["reasons"] == ["umbrella: rain 70% at 09:00"]
del tray4.data["tags"]["U"]
on_tray("L", "O")
tray4.data["event_overrides"][tray4.event_key(WORK)] = ["umbrella"]                     # list items without a tag: as before
s = tray4.status(NOW)
assert s["state"] == "MISSING" and s["no_tag"] == ["umbrella"] and s["reminders"] == [], s
tray4.data["event_overrides"].clear()

# ---------- notifications: reminders included, once per day per item set ----------
print("notifications")
sent.clear(); tray4.reminded.clear(); tray4.data["notified"] = {}
check = t(2026, 11, 12, 7, 46)                                                          # 15 min before Work 08:00
on_tray("L", "O", now=check)
tray4.calendar_check(check)
assert sent == ["Work 08:00. Remember: umbrella (rain 70% at 09:00)."], sent
tray4.calendar_check(check)
assert len(sent) == 1
tray4.reminded.clear(); sent.clear(); tray4.data["notified"] = {}
on_tray("L", now=check)
tray4.calendar_check(check)
assert sent == ["Work 08:00. missing from tray: bottle. Remember: umbrella (rain 70% at 09:00)."], sent
EVE = t(2026, 11, 11, 21, 0)                                                            # evening before: checks the 12th
on_tray("L", "O", now=EVE)
tray4.reminded.clear(); sent.clear()
tray4.evening_check(EVE)
assert sent == ["Tomorrow: Work 08:00. On the tray: laptop, bottle. Remember: umbrella (rain 70% at 09:00)."], sent
tray4.evening_check(EVE)
assert len(sent) == 1                                                                   # same set, same day: not again
on_tray("L", now=EVE)
tray4.evening_check(EVE)
assert sent[-1] == "Tomorrow: Work 08:00. On the tray: laptop. Missing: bottle.", sent  # reminder not repeated
on_tray("L", "O")

# ---------- fetch failure keeps the last data; stale cache = no weather rules ----------
print("fetch failure, stale cache")
before = (list(tray4.weather["hours"]), tray4.weather["fetched_at"])
tray4.urllib.request.urlopen = _no_network
assert not tray4.fetch_weather() and (tray4.weather["hours"], tray4.weather["fetched_at"]) == before
serve(b"<html>error</html>"); assert not tray4.fetch_weather()
serve({"hourly": {"time": ["2026-11-12T00:00"]}}); assert not tray4.fetch_weather()   # missing columns
assert (tray4.weather["hours"], tray4.weather["fetched_at"]) == before
tray4.urllib.request.urlopen = _no_network
assert items() == [("umbrella", "rain 70% at 09:00")]
assert not any("12.3456" in l or "65.4321" in l or "api.open-meteo" in l for l in logged), "coordinates/URL logged"
cached = json.load(open(tray4.WEATHER_CACHE))
cached["fetched_at"] = time.time() - 7 * 3600                                           # older than weather_stale_hours (6)
open(tray4.WEATHER_CACHE, "w").write(json.dumps(cached))
tray4.weather.update(hours=[], fetched_at=None)
tray4.load_weather_cache()
assert tray4.weather["hours"] and tray4.weather_stale()
w = tray4.weather_for([WORK], t(2026, 11, 12, 7))
assert w["items"] == [] and w["note"] == "unavailable"
s = tray4.status(NOW)
assert s["reminders"] == [] and s["weather_unavailable"] and s["state"] == "READY"
assert tray4.v1_health(NOW)["weather"]["stale"] and tray4.state(NOW)["weather"]["note"] == "unavailable"
tray4.config["weather_stale_hours"] = 8
assert not tray4.weather_stale() and items() == [("umbrella", "rain 70% at 09:00")]
del tray4.config["weather_stale_hours"]
assert tray4.weather_for([ev("f", "Work", 8, 9)], t(2026, 11, 12, 7)) and tray4.window_rows(t(2026, 11, 30, 8), t(2026, 11, 30, 9))[1] == "unavailable"
open(tray4.WEATHER_CACHE, "w").write("{broken")
tray4.load_weather_cache()                                                              # ignored, no crash

# ---------- weather_override ----------
print("weather_override")
tray4.weather.update(hours=[], fetched_at=None)                                         # no real data at all
for ov, want in (("rain", ["umbrella"]), ("hot", ["sunglasses", "sunscreen"]), ("cold", ["scarf", "gloves"]),
                 ("off", []), ("none", []), ("bogus", [])):
    tray4.config["weather_override"] = ov
    got = [n for n, _ in items()]
    assert got == want, (ov, got)
    s = tray4.status(NOW)
    assert s["weather_simulated"] == ({"rain": "rain", "hot": "hot", "cold": "cold", "off": "off"}.get(ov)), (ov, s)
    print(f"  {ov:6} -> {got or 'nothing'}  (note: {tray4.weather_for([WORK], t(2026, 11, 12, 7))['note']})")
assert tray4.weather_for([WORK], t(2026, 11, 12, 7))["note"] == "unavailable"                               # none + no data
tray4.config["weather_override"] = "off"
assert tray4.weather_for([WORK], t(2026, 11, 12, 7))["note"] == "off"
tray4.config["weather_override"] = "rain"
assert tray4.state(NOW)["weather"]["simulated"] == "rain"

# ---------- API shapes ----------
print("API")
plan = tray4.v1_plan(NOW)
assert plan["reminders"] == [{"name": "umbrella", "reason": "rain 80% at 08:00"}], plan["reminders"]
wx = plan["weather"]
assert wx["simulated"] == "rain" and wx["now"]["precipitation_probability"] == 80
assert wx["window"]["min"]["temperature_2m"] == 9 and wx["window"]["max"]["precipitation"] == 2.0
assert wx["reasons"] == ["umbrella: rain 80% at 08:00"]
tray4.config["weather_override"] = "cold"
assert tray4.v1_plan(NOW)["weather"]["reasons"] == ["scarf, gloves: 2 degrees at 08:00"]
load()
h = tray4.v1_health(NOW)["weather"]
assert set(h) == {"fetched_at", "age_minutes", "stale"} and not h["stale"]
assert "12.3456" not in json.dumps(tray4.v1_health(NOW)) and "12.3456" not in json.dumps(tray4.v1_plan(NOW))
st = tray4.state(NOW)["weather"]                                    # the page shows the location (asked for), /api/v1 doesn't
assert st["location"] == {"name": "", "latitude": 12.3456, "longitude": 65.4321} and st["now"] and st["range"]["from"] == "08:00"
tray4.config["weather_override"] = "none"
r = tray4.settings({"weather_override": "HOT"})
assert r["ok"] and tray4.config["weather_override"] == "hot"
assert not tray4.settings({"weather_override": "snow"})["ok"]
tray4.config["weather_override"] = "none"

# ---------- rules file and validation ----------
print("location")
logged.clear(); tray4.weather_wake.clear()
assert not tray4.settings({"latitude": "91", "longitude": "10"})["ok"] and not tray4.settings({"latitude": "x", "longitude": "1"})["ok"]
assert not tray4.settings({"latitude": "46.6"})["ok"]                                  # both needed
r = tray4.settings({"latitude": "46.61", "longitude": "13.85", "location_name": "Villach"})
assert r["ok"] and tray4.location() == {"name": "Villach", "latitude": 46.61, "longitude": 13.85}
assert tray4.settings({"latitude": "46.61", "longitude": "13.85", "location_name": "<img src=x onerror=alert(1)>Villach"})["ok"]
assert tray4.location()["name"] == "img src=x onerror=alert(1)Villach"                 # no markup stored
tray4.settings({"latitude": "46.61", "longitude": "13.85", "location_name": "Villach"})
assert tray4.weather_wake.is_set() and tray4.weather["fetched_at"] is None             # refetch now, old forecast dropped
saved = json.load(open(tray4.CONFIG_FILE))
assert (saved["latitude"], saved["longitude"], saved["location_name"]) == (46.61, 13.85, "Villach")
assert not any("46.61" in l or "Villach" in l for l in logged), "location logged"
serve({"results": [{"name": "Villach", "admin1": "Carinthia", "country": "Austria", "latitude": 46.61, "longitude": 13.85}]})
g = tray4.geocode("Villach")
assert g["ok"] and g["results"] == [{"name": "Villach", "region": "Carinthia, Austria", "latitude": 46.61, "longitude": 13.85}]
assert "name=Villach" in asked[-1][0] and asked[-1][1] == 10 and asked[-1][0].startswith(tray4.GEOCODE_URL)
serve({}); assert tray4.geocode("Nowhere") == {"ok": True, "msg": "No place found", "results": []}
tray4.urllib.request.urlopen = _no_network
assert not tray4.geocode("Villach")["ok"] and not tray4.geocode("x")["ok"]
assert not any("Villach" in l or "Nowhere" in l for l in logged), "search logged"
tray4.config.pop("location_name"); tray4.config.update(latitude=12.3456, longitude=65.4321)

print("rules editing")
assert tray4.parse_condition("precipitation_probability >= 50% OR precipitation >= 0.5 mm") == \
    [[("precipitation_probability", ">=", 50.0)], [("precipitation", ">=", 0.5)]]
for bad in ("", "rain >= 50", "uv_index => 6", "uv_index >= six", "uv_index >= 6 and"):
    try:
        tray4.parse_condition(bad); raise AssertionError(bad)
    except ValueError:
        pass
assert not tray4.save_rules({"rules": [{"name": "x", "condition": "snow > 1", "items": ["boots"]}]})["ok"]
assert not tray4.save_rules({"rules": [{"name": "x", "condition": "uv_index > 1", "items": []}]})["ok"]
assert not tray4.save_rules({"rules": "nope"})["ok"]
assert tray4.api("/api/rules", {"rules": [{"name": "freezing", "condition": "temperature_2m < 3", "items": "hat, gloves"}]})["ok"]
assert json.load(open(tray4.RULES_FILE)) == [{"name": "freezing", "condition": "temperature_2m < 3", "items": ["hat", "gloves"]}]
assert tray4.load_rules() == tray4.rules
tray4.config["weather_override"] = "cold"
assert [n for n, _ in items()] == ["hat", "gloves"]
print("OK")
