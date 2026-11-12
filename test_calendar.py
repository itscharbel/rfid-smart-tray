# Local check of calendar, list, day-plan, notification and settings logic (no Pi hardware needed).
# Needs: pip install icalendar recurring-ical-events    Run: python3 test_calendar.py
import threading, json, os, re, stat, tempfile, time
from datetime import datetime
from zoneinfo import ZoneInfo
# Never read the real tray_config.json (secrets): point tray4 at an empty temp folder before importing it.
import tempfile as _tf
os.environ["TRAY_CONFIG"] = os.path.join(_tf.mkdtemp(), "tray_config.json")
import tray4
if tray4.CONFIG_FILE == "/home/pi/tray_config.json":
    raise SystemExit("refusing to run: tests must not use the real tray_config.json")

# Never touch the network or the real files: block HTTP, stub ntfy and saving
def _no_network(*a, **k):
    raise AssertionError("network call during tests")
tray4.urllib.request.urlopen = _no_network
sent = []
tray4.notify = lambda msg, click=None: sent.append(msg) or True
tray4.save = lambda: None

VIE = ZoneInfo("Europe/Vienna")
t = lambda *a: datetime(*a, tzinfo=VIE).timestamp()

def put_on_tray(now, *uids):
    """Place tags on random readers (position must not matter)."""
    for r in tray4.SLOTS:
        tray4.last_seen[r] = 0
    for r, uid in zip(reversed(tray4.SLOTS), uids):
        tray4.last_uid[r], tray4.last_seen[r] = uid, now

# ---------- ICS parsing: timezone, recurrence, notes ----------
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VTIMEZONE
TZID:Europe/Vienna
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:gym
SUMMARY:Gym
DTSTART;TZID=Europe/Vienna:20261020T183000
DTEND;TZID=Europe/Vienna:20261020T200000
RRULE:FREQ=WEEKLY;BYDAY=TU,TH
EXDATE;TZID=Europe/Vienna:20261029T183000
DESCRIPTION:Bring: Key\\, wallet\\nTravel: 10
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Bring: umbrella
TRIGGER:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:gym
RECURRENCE-ID;TZID=Europe/Vienna:20261103T183000
SUMMARY:Gym (moved)
DTSTART;TZID=Europe/Vienna:20261103T200000
DESCRIPTION:Bring: key
END:VEVENT
BEGIN:VEVENT
UID:dinner
SUMMARY:Dinner
DTSTART:20261028T180000Z
DESCRIPTION:Hi<br>porta: Wallet, umbrella<br>Viaggio: 5
END:VEVENT
BEGIN:VEVENT
UID:floating
SUMMARY:Dentist
DTSTART:20261030T090000
DESCRIPTION:Bring: card
END:VEVENT
BEGIN:VEVENT
UID:meeting
SUMMARY:Meeting
DTSTART:20261028T100000Z
DESCRIPTION:nothing to bring
END:VEVENT
BEGIN:VEVENT
UID:allday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261029
DESCRIPTION:Bring: suitcase
END:VEVENT
BEGIN:VEVENT
UID:cancelled
SUMMARY:Cancelled
STATUS:CANCELLED
DTSTART:20261029T100000Z
DESCRIPTION:Bring: key
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")

evs = tray4.parse_ics(ICS, t(2026, 10, 22), t(2026, 11, 6))
got = [(e["title"], datetime.fromtimestamp(e["start"], VIE).strftime("%d/%m %H:%M"), e["bring"], e["travel"])
       for e in evs]
assert got == [
    ("Gym", "22/10 18:30", ["Key", "wallet"], 10),               # CEST
    ("Gym", "27/10 18:30", ["Key", "wallet"], 10),               # CET after 25/10: still 18:30 local
    ("Meeting", "28/10 11:00", [], 0),                           # no "Bring:": kept, presets may still apply
    ("Dinner", "28/10 19:00", ["Wallet", "umbrella"], 5),        # UTC -> Vienna, HTML notes, Italian "porta:"/"Viaggio:"
    ("Dentist", "30/10 09:00", ["card"], 0),                     # floating time = Vienna
    ("Gym (moved)", "03/11 20:00", ["key"], 0),                  # override of 03/11 18:30
    ("Gym", "05/11 18:30", ["Key", "wallet"], 10),
], got  # 29/10 EXDATE; all-day and cancelled skipped; VALARM "Bring:" ignored
assert evs[1]["end"] - evs[1]["start"] == 90 * 60                # DTEND kept on each occurrence
assert evs[2]["end"] == evs[2]["start"] or evs[2]["end"] > evs[2]["start"]
print("0) ICS parsing: OK")

# ---------- per-event lists: presets, "Bring:" line, overrides ----------
LISTS_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:gym-weekly
SUMMARY:GYM
DTSTART;TZID=Europe/Vienna:20261102T180000
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:gym-bring
SUMMARY:Gym with Anna
DTSTART;TZID=Europe/Vienna:20261103T070000
DESCRIPTION:Bring: key, Towel
END:VEVENT
BEGIN:VEVENT
UID:dinner
SUMMARY:Dinner with Marco
DTSTART;TZID=Europe/Vienna:20261104T200000
DESCRIPTION:Porta: wallet, key
END:VEVENT
BEGIN:VEVENT
UID:alias
SUMMARY:Sport session
DTSTART;TZID=Europe/Vienna:20261105T190000
END:VEVENT
BEGIN:VEVENT
UID:nomatch
SUMMARY:Gymnastics show
DTSTART;TZID=Europe/Vienna:20261106T100000
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")

tray4.data.update(tags={"U1": "key", "U2": "wallet", "U3": "bottle"}, notified={}, event_overrides={},
                  presets={"gym": {"aliases": ["sport"], "items": ["bottle", "towel", "shoes"]}})
tray4.cal["events"] = evs = tray4.parse_ics(LISTS_ICS, t(2026, 11, 1), t(2026, 11, 30))
by = lambda title, day: next(e for e in evs if e["title"] == title
                             and datetime.fromtimestamp(e["start"], VIE).day == day)
def show(label):
    print(f"\n{label}")
    for e in evs:
        items, override, names = tray4.resolve(e)
        src = "override" if override else "+".join(names + (["Bring:"] if e["bring"] else [])) or "-"
        print(f'  {tray4.event_key(e):24} {e["title"]:24} [{src}] {items}')

show("1) preset gym (alias: sport) + Bring: lines")
assert tray4.resolve(by("GYM", 2))[0] == ["bottle", "towel", "shoes"]                  # preset only
assert tray4.resolve(by("Gym with Anna", 3))[0] == ["bottle", "towel", "shoes", "key"]  # union, "Towel" deduped
assert tray4.resolve(by("Dinner with Marco", 4))[0] == ["wallet", "key"]               # Italian "Porta:" still works
assert tray4.resolve(by("Sport session", 5)) == (["bottle", "towel", "shoes"], False, ["gym"])
assert tray4.resolve(by("Gymnastics show", 6))[0] == []                                 # whole word only

gym9 = tray4.event_key(by("GYM", 9))
assert tray4.api("/api/event_items", {"key": gym9, "items": ["key", "Phone"], "scope": "event"})["ok"]
show("2) override on GYM 09/11 only")
assert tray4.resolve(by("GYM", 9)) == (["key", "Phone"], True, [])
assert tray4.resolve(by("GYM", 2))[0] == ["bottle", "towel", "shoes"]                  # other occurrences untouched

anna = tray4.event_key(by("Gym with Anna", 3))
r = tray4.api("/api/event_items", {"key": anna, "items": ["bottle", "key", "Towel", "headphones"],
                                   "scope": "preset", "preset": "GYM"})
assert r["ok"] and r["msg"] == "List “gym” updated", r
assert tray4.data["presets"]["gym"]["items"] == ["bottle", "headphones"]  # Bring: items stay in the notes
show("3) 'all GYM events' edit from Gym with Anna (removed shoes, added headphones)")
assert tray4.resolve(by("GYM", 16))[0] == ["bottle", "headphones"]
assert tray4.resolve(by("GYM", 9))[0] == ["key", "Phone"]                              # override still wins

dinner = tray4.event_key(by("Dinner with Marco", 4))
assert not tray4.api("/api/event_items", {"key": dinner, "items": ["x"], "scope": "preset", "preset": "pizza"})["ok"]
r = tray4.api("/api/event_items", {"key": dinner, "items": ["wallet", "key", "gift"], "scope": "preset", "preset": "Dinner"})
assert r["ok"] and tray4.data["presets"]["dinner"] == {"aliases": [], "items": ["gift"]}, r
assert tray4.api("/api/event_items", {"key": gym9, "items": [], "scope": "reset"})["ok"]
show("4) new preset 'dinner' from the title keyword; GYM 09/11 override reset")
assert tray4.resolve(by("Dinner with Marco", 4))[0] == ["gift", "wallet", "key"]
assert tray4.resolve(by("GYM", 9))[0] == ["bottle", "headphones"]

assert not tray4.save_preset({"name": "run", "aliases": "sport", "items": []})["ok"]   # alias already used by gym
assert tray4.save_preset({"name": "training", "aliases": "sport, gym", "items": ["bottle"], "old_name": "gym"})["ok"]
assert "gym" not in tray4.data["presets"] and tray4.resolve(by("GYM", 2))[0] == ["bottle"]  # renamed, alias still matches
assert tray4.delete_preset({"name": "training"})["ok"] and tray4.resolve(by("GYM", 2))[0] == []
assert not tray4.delete_preset({"name": "training"})["ok"]

# ---------- day plan: Work (pc + bottle) in the morning + Gym (towel + bottle) in the evening ----------
NOW = t(2026, 11, 2, 7, 0)
tray4.data.update(tags={"U1": "pc", "U2": "bottle", "U3": "towel", "U4": "keys"}, notified={}, event_overrides={},
                  presets={"gym": {"aliases": [], "items": ["towel", "bottle"]},
                           "work": {"aliases": [], "items": ["pc", "bottle"]}})
tray4.cal["events"] = [
    {"uid": "w", "title": "Work", "start": t(2026, 11, 2, 9, 0), "bring": [], "travel": 0},
    {"uid": "m", "title": "Meeting", "start": t(2026, 11, 2, 12, 0), "bring": [], "travel": 0},   # no items
    {"uid": "g", "title": "Gym", "start": t(2026, 11, 2, 20, 0), "bring": ["water card"], "travel": 0},
    {"uid": "w2", "title": "Work", "start": t(2026, 11, 3, 9, 0), "bring": [], "travel": 0},     # tomorrow
]
sent.clear()
put_on_tray(NOW, "U1", "U4")                                     # pc + keys
problems, lbl = tray4.leaving_check(NOW)
print("\n5) day plan at 07:00:", lbl, problems)
assert lbl == "Work 09:00 + Gym 20:00"
assert problems == ["missing bottle", "missing towel", "water card has no tag"], problems
assert sent == ["Don't leave yet (Work 09:00 + Gym 20:00): missing bottle; missing towel; water card has no tag"]
st = tray4.state(NOW)
assert [i["name"] for i in st["today"]["items"]] == ["pc", "bottle", "towel", "water card"]
assert len(st["today"]["keys"]) == 2 and st["today"]["check"] == "08:45"
day = tray4.state(t(2026, 11, 2, 13, 0))["day"]                 # page's "Today": all of it, past events too
assert [e["title"] for e in day] == ["Work", "Meeting", "Gym"], day
assert tray4.start_of_day(t(2026, 11, 2, 13, 0)) == t(2026, 11, 2)

put_on_tray(NOW, "U1", "U2", "U3")                               # pc + bottle + towel, any readers
tray4.cal["events"][2]["bring"] = []
assert tray4.leaving_check(NOW) == ([], "Work 09:00 + Gym 20:00")
put_on_tray(t(2026, 11, 2, 13, 0), "U2")                         # after work only the gym is left
assert tray4.leaving_check(t(2026, 11, 2, 13, 0)) == (["missing towel"], "Gym 20:00")
assert tray4.leaving_check(t(2026, 11, 2, 21, 0)) == ([], None)  # nothing left today (tomorrow not included)
tray4.cal["events"][0]["end"] = t(2026, 11, 2, 17, 0)            # Work 09:00-17:00
assert tray4.leaving_check(t(2026, 11, 2, 13, 0)) == (["missing pc", "missing towel"], "Work 09:00 + Gym 20:00")  # still at work: pc counts
assert [tray4.event_key(e) for e in tray4.rest_of_day(t(2026, 11, 2, 17, 30))] == [tray4.event_key(tray4.cal["events"][2])]
del tray4.cal["events"][0]["end"]

# notifications: one per event, each covering that event + the later ones that day
sent.clear()
at_work = t(2026, 11, 2, 8, 46)
put_on_tray(at_work, "U1")                                       # only the pc
tray4.calendar_check(at_work)
tray4.calendar_check(at_work)                                    # no repeat
assert sent == ["Work 09:00 + Gym 20:00. missing from tray: bottle, towel"], sent
at_gym = t(2026, 11, 2, 19, 50)
put_on_tray(at_gym, "U2")                                        # towel still missing, but the day's reminder went out at 08:45
tray4.calendar_check(at_gym)
assert len(sent) == 1 and {k.split("@")[0] for k in tray4.data["notified"]} == {"w"}
st = tray4.state(at_gym)
assert st["today"]["keys"] and st["today"]["check"] is None     # page: no "reminder at 19:45" promise
print("6) day plan + notifications: OK")

# ---------- bag lifted: everything leaves the tray at once -> check what was in it ----------
def tray_seq(*steps):
    """steps: (seconds after NOW, uids on the tray). Runs tray_changed and waits for the check thread."""
    tray4.shown.clear(); tray4.removed_at.clear()
    for dt, uids in steps:
        tray4.tray_changed({u: tray4.data["tags"].get(u) for u in uids}, NOW + dt)
    for th in threading.enumerate():
        if th is not threading.current_thread() and th.daemon and th.name.startswith("Thread"):
            th.join(2)

sent.clear()
tray_seq((0, ["U1", "U2"]), (60, []))                            # pc + bottle lifted together, towel forgotten
assert sent == ["Bag lifted without everything (Work 09:00 + Gym 20:00): missing towel"], sent
sent.clear()
tray_seq((0, ["U1", "U2", "U3"]), (60, []))                      # everything in the bag: silent
assert sent == [], sent
tray_seq((0, ["U1"]), (60, []))                                  # one thing taken off: not a bag
tray_seq((0, ["U1", "U2"]), (60, ["U1"]), (65, []))              # taken off one by one: not a lift
assert sent == [], sent
print("7) bag lifted: OK")

# ---------- settings: config file 600, webcal converted, API never returns full values ----------
with tempfile.TemporaryDirectory() as d:
    tray4.CONFIG_FILE = os.path.join(d, "tray_config.json")
    tray4.config.update(ntfy_topic="", ics_url="")
    tray4.save_config()
    assert stat.S_IMODE(os.stat(tray4.CONFIG_FILE).st_mode) == 0o600
    assert not tray4.api("/api/settings", {"ntfy_topic": "bad topic!"})["ok"]
    assert not tray4.api("/api/settings", {"lead_minutes": "abc"})["ok"]
    r = tray4.api("/api/settings", {"ntfy_topic": "https://ntfy.sh/tray-secret-abcd",
                                    "ics_url": "webcal://cal.example/private-xyz/basic.ics", "lead_minutes": "20"})
    assert r["ok"], r
    saved = json.load(open(tray4.CONFIG_FILE))
    assert saved == {"ntfy_topic": "tray-secret-abcd", "ics_url": "https://cal.example/private-xyz/basic.ics",
                     "lead_minutes": 20}, "saved settings differ (values not printed)"
    assert stat.S_IMODE(os.stat(tray4.CONFIG_FILE).st_mode) == 0o600
    tray4.api("/api/settings", {"lead_minutes": "15"})  # empty fields keep their values
    assert json.load(open(tray4.CONFIG_FILE))["ntfy_topic"] == "tray-secret-abcd"
    s = json.dumps(tray4.state(NOW))
    assert "secret" not in s and "private" not in s, s
    assert tray4.state(NOW)["ntfy"] == {"set": True, "last4": "abcd"}

# ---------- overall status (LEDs + /api/v1) ----------
tray4.cal.update(state="ok", updated=NOW, events=[             # the settings test above cleared them
    {"uid": "w", "title": "Work", "start": t(2026, 11, 2, 9, 0), "bring": [], "travel": 0},
    {"uid": "m", "title": "Meeting", "start": t(2026, 11, 2, 12, 0), "bring": [], "travel": 0},
    {"uid": "g", "title": "Gym", "start": t(2026, 11, 2, 20, 0), "bring": [], "travel": 0}])
put_on_tray(NOW, "U1", "U2")                                     # pc + bottle, towel missing for the gym
s = tray4.status(NOW)
assert s["state"] == "MISSING" and s["missing"] == ["towel"] and s["no_tag"] == [], s
assert s["next_event"]["title"] == "Work" and s["next_event"]["start"].startswith("2026-11-02T09:00"), s
put_on_tray(NOW, "U1", "U2", "U3")
assert tray4.status(NOW)["state"] == "READY"
tray4.cal["updated"] = t(2026, 11, 2, 21, 0)
assert tray4.status(t(2026, 11, 2, 21, 0))["state"] == "IDLE"   # nothing left today (fresh data)
tray4.cal.update(state="unreachable", updated=NOW)
assert tray4.status(NOW)["state"] == "READY"                     # failed fetch, fresh copy: not stale
H6 = 6 * 3600 + 1
put_on_tray(NOW + H6, "U1", "U2", "U3")
assert tray4.status(NOW + H6)["state"] == "STALE" and tray4.v1_health(NOW + H6)["calendar"]["stale"]
put_on_tray(t(2026, 11, 2, 21, 0))
assert tray4.status(t(2026, 11, 2, 21, 0))["state"] == "STALE"  # stale beats IDLE: the plan may be wrong
put_on_tray(NOW + H6, "U1")
assert tray4.status(NOW + H6)["state"] == "MISSING"              # ...but what's surely missing still shows
put_on_tray(NOW, "U1", "U2", "U3")
tray4.config["stale_after_hours"] = 0.01                         # 36 s, configurable
assert tray4.cal_stale(NOW + 37) and not tray4.cal_stale(NOW + 35)
st = tray4.state(NOW + 37)
assert st["stale"] and st["age_minutes"] == 0 and st["calendar_updated"].startswith("2026-11-02T07:00"), st
tray4.config["stale_after_hours"] = "bad"
assert tray4.stale_after() == 6 * 3600
del tray4.config["stale_after_hours"]
sent.clear()
put_on_tray(NOW + 7 * 3600, "U1")
tray4.leaving_check(NOW + 7 * 3600)
assert sent and sent[0].endswith(" (calendar data 7.0 h old)"), sent
tray4.cal.update(state="ok")
put_on_tray(NOW, "U1", "U2", "U3")

items = {i["name"]: i for i in tray4.v1_items(NOW)["items"]}
assert items["towel"]["on_tray"] and items["towel"]["slot"] in tray4.SLOTS and items["towel"]["expected"]
assert not items["keys"]["expected"] and not items["keys"]["on_tray"] and items["keys"]["slot"] is None

tray4.cal["events"][2]["bring"] = ["bottle", "water card"]       # bottle from the gym list AND the notes
plan = tray4.v1_plan(NOW)
gym = next(e for e in plan["events"] if e["title"] == "Gym")
assert gym["items"] == [{"name": "towel", "reasons": ["preset:gym"]},
                        {"name": "bottle", "reasons": ["preset:gym", "notes"]},
                        {"name": "water card", "reasons": ["notes"]}], gym
work = next(e for e in plan["events"] if e["title"] == "Work")
assert work["items"] == [{"name": "pc", "reasons": ["preset:work"]}, {"name": "bottle", "reasons": ["preset:work"]}], work
assert {"name": "water card", "status": "no_tag"} in plan["required"] and len(plan["events"]) == 3

assert tray4.on_lan("172.20.10.7") and tray4.on_lan("127.0.0.1") and tray4.on_lan("100.101.102.103")
assert not tray4.on_lan("8.8.8.8")

import led
with tempfile.TemporaryDirectory() as d:
    led.BASE = d
    for name in ("ACT", "PWR"):
        os.mkdir(os.path.join(d, name))
    read = lambda n, a: open(os.path.join(d, n, a)).read()
    led.set_state("MISSING")
    assert (read("ACT", "brightness"), read("PWR", "brightness"), read("PWR", "trigger")) == ("0", "1", "none")
    led.set_state("STALE")
    assert read("ACT", "brightness") == "1" and read("PWR", "trigger") == "timer"
    led.restore()
    assert (read("ACT", "trigger"), read("PWR", "trigger")) == ("mmc0", "default-on")
    logged = []
    led.BASE = os.path.join(d, "missing")
    led.set_state("READY", logged.append); led.set_state("IDLE", logged.append)  # no LEDs: log once, no crash
    assert len(logged) == 1, logged
print("8) status, /api/v1 and LEDs: OK")

# ---------- calendar cache: only good responses are kept, loaded at startup ----------
class Resp:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return self.body

def fetch_with(result, now):
    def fake(req, timeout):
        if isinstance(result, Exception):
            raise result
        return Resp(result)
    tray4.urllib.request.urlopen = fake
    try:
        tray4.fetch_calendar("https://cal.example/private.ics", now)
    finally:
        tray4.urllib.request.urlopen = _no_network

with tempfile.TemporaryDirectory() as d:
    tray4.CACHE_ICS, tray4.CACHE_META = os.path.join(d, "c.ics"), os.path.join(d, "c.json")
    tray4.CONFIG_FILE = os.path.join(d, "tray_config.json")
    tray4.config["ics_url"] = "https://cal.example/private.ics"
    tray4.cached["text"] = None
    good = ICS.encode()
    fetch_with(good, t(2026, 10, 22, 8, 0))
    assert tray4.cal["state"] == "ok" and open(tray4.CACHE_ICS, "rb").read() == good
    assert json.load(open(tray4.CACHE_META)) == {"fetched_at": t(2026, 10, 22, 8, 0)}
    assert stat.S_IMODE(os.stat(tray4.CACHE_ICS).st_mode) == 0o600 and not os.path.exists(tray4.CACHE_ICS + ".tmp")
    n = len(tray4.cal["events"])
    bad = [tray4.urllib.error.HTTPError("u", 500, "x", {}, None), OSError("Network is unreachable"),
           b"", b"   \r\n", b"<html>login</html>", b"BEGIN:VCALENDAR\nthis is not ical"]
    for i, r in enumerate(bad):
        fetch_with(r, t(2026, 10, 22, 9, i))
        assert tray4.cal["state"] in ("unreachable", "invalid"), (r, tray4.cal["state"])
        assert open(tray4.CACHE_ICS, "rb").read() == good, r                     # never overwritten
        assert json.load(open(tray4.CACHE_META))["fetched_at"] == t(2026, 10, 22, 8, 0)
        assert len(tray4.cal["events"]) == n and tray4.cal["updated"] == t(2026, 10, 22, 8, 0)  # old events kept

    tray4.cal.update(state="loading", events=[], updated=None)       # restart, then load before any fetch
    tray4.load_cache(t(2026, 10, 22, 10, 0))
    assert tray4.cal["updated"] == t(2026, 10, 22, 8, 0) and len(tray4.cal["events"]) == n > 0
    assert not tray4.cal_stale(t(2026, 10, 22, 10, 0))               # loading + cache 2 h old: fresh
    fetch_with(OSError("offline"), t(2026, 10, 22, 14, 1))           # offline boot: cache kept, stale after 6 h
    assert len(tray4.cal["events"]) == n and tray4.cal_stale(t(2026, 10, 22, 14, 1))
    tray4.api("/api/settings", {"ics_url": "https://cal.example/other.ics"})
    assert not os.path.exists(tray4.CACHE_ICS)                       # new link: old calendar's cache gone
    tray4.cal.update(state="loading", events=[], updated=None)
    tray4.load_cache(t(2026, 10, 22, 10, 0))                         # nothing to load: no crash
    assert tray4.cal["updated"] is None
print("9) calendar cache: OK")

# ---------- /api/v1 access: status/health open, plan/items/stream need the token, /display gets a session ----------
import urllib.request as ur, urllib.error
from http.server import ThreadingHTTPServer
tray4.config["api_token"] = TOKEN = "test-token-123"
tray4.current.update(status=tray4.status(NOW), version=1)
srv = ThreadingHTTPServer(("127.0.0.1", 0), tray4.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

def get(path, headers=None, read=True):
    req = ur.Request(BASE + path, headers=headers or {})
    try:
        r = opener.open(req, timeout=3)
        return r.status, dict(r.headers), (r.readline() if path.endswith("stream") else r.read()) if read else b""
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

opener = ur.build_opener()
for p in ("/api/v1/status", "/api/v1/health"):
    assert get(p)[0] == 200, p
for p in ("/api/v1/plan", "/api/v1/items", "/api/v1/stream"):
    code, h, body = get(p)
    assert code == 401 and h["Access-Control-Allow-Origin"] == "*", (p, code)
    assert get(p, {"Authorization": "Bearer wrong"})[0] == 401
    assert get(p, {"Authorization": f"Bearer {TOKEN}"})[0] == 200, p
code, h, page = get("/display")
assert code == 200 and TOKEN.encode() not in page                  # token never in the page
cookie = h["Set-Cookie"].split(";")[0]
assert "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"] and TOKEN not in cookie
code, _, first = get("/api/v1/stream", {"Cookie": cookie})          # what the display's EventSource does
assert code == 200 and first.startswith(b"data: {"), first
assert get("/api/v1/plan", {"Cookie": cookie})[0] == 401            # the display session opens the stream only
assert get("/api/v1/stream", {"Cookie": "tray_session=forged"})[0] == 401
assert get("/")[0] == 200 and get("/api/state")[0] == 200            # web UI untouched
tray4.config["api_token"] = ""
assert get("/api/v1/plan", {"Authorization": "Bearer "})[0] == 401  # no token configured: nothing gets in
import socket
def raw_post(length):
    """POST /api/bag with a hand-written Content-Length; -> status code (the server must answer, not hang)."""
    with socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=3) as c:
        c.sendall(f"POST /api/bag HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n\r\n".encode())
        return int(c.recv(64).split()[1])
for bad in ("-1", "abc", "8001"):
    assert raw_post(bad) == 413, bad                                # rejected at once, no waiting for the body
srv.shutdown()
print("10) /api/v1 access: OK")

# ---------- events.csv: queued writes, ms timestamps, rotation; nothing written before the writer starts ----------
import csv, queue
with tempfile.TemporaryDirectory() as d:
    tray4.EVENTS_FILE, tray4.EVENTS_MAX = os.path.join(d, "events.csv"), 300
    tray4.events_q = None
    tray4.log_event("slot", "R1", "keys", "", "placed X")             # writer not started (tests): no file
    assert not os.path.exists(tray4.EVENTS_FILE)
    tray4.events_q = queue.Queue()
    threading.Thread(target=tray4.events_writer, daemon=True).start()
    tray4.log_event("slot", "R1", "keys", "", "placed 33:46:64:F5")
    tray4.log_event("state", state="MISSING", detail="plan_for=today missing=towel no_tag=")
    tray4.log_event("notify", detail="Don't leave yet (Gym 18:00): missing towel, \"quoted\" [sent]")
    deadline = time.time() + 3
    while tray4.events_q.unfinished_tasks and not os.path.exists(tray4.EVENTS_FILE) and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    rows = list(csv.reader(open(tray4.EVENTS_FILE)))
    assert rows[0] == ["ts", "type", "slot", "name", "state", "detail"] and len(rows) == 4, rows
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+0[12]:00", rows[1][0]), rows[1][0]
    assert rows[3][5].endswith('"quoted" [sent]')                     # commas and quotes survive (csv)
    for i in range(10):                                               # past EVENTS_MAX: rotated
        tray4.log_event("state", state="READY", detail="x" * 40)
        time.sleep(0.05)
    time.sleep(0.3)
    assert os.path.exists(tray4.EVENTS_FILE + ".1") and open(tray4.EVENTS_FILE).readline().startswith("ts,type")
    tray4.events_q = None
print("11) events.csv: OK")
print("OK")
