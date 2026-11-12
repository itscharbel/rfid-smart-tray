# Evening mode, driven by TRAY_FAKE_NOW (no Pi hardware, no network). Run: python3 test_evening.py
# Calendar: Wed 11/11 Gym 20:00 · Thu 11/12 Work 09:00 + Gym 19:00 · Fri 11/13 nothing.
import os, threading, time
os.environ["TRAY_FAKE_NOW"] = "2026-11-11T21:30:00"
# Never read the real tray_config.json (secrets): point tray4 at an empty temp folder before importing it.
import tempfile as _tf
os.environ["TRAY_CONFIG"] = os.path.join(_tf.mkdtemp(), "tray_config.json")
import tray4
if tray4.CONFIG_FILE == "/home/pi/tray_config.json":
    raise SystemExit("refusing to run: tests must not use the real tray_config.json")
from datetime import datetime
from zoneinfo import ZoneInfo

def _no_network(*a, **k):
    raise AssertionError("network call during tests")
tray4.urllib.request.urlopen = _no_network
sent = []
tray4.notify = lambda msg, click=None: sent.append(msg) or True
tray4.save = lambda: None
tray4.log = lambda msg: None

VIE = ZoneInfo("Europe/Vienna")
t = lambda *a: datetime(*a, tzinfo=VIE).timestamp()
tray4.SLOTS = tuple(f"R{i}" for i in range(1, 7))   # bag + 3 things: more than the 3 prototype readers
tray4.last_seen = {s: 0.0 for s in tray4.SLOTS}
tray4.last_uid = {s: None for s in tray4.SLOTS}
TAGS = {"B": "bag", "L": "laptop", "O": "bottle", "T": "towel", "K": "keys"}
tray4.data.update(tags=dict(TAGS), unnamed={}, notified={}, event_overrides={},
                  presets={"work": {"aliases": [], "items": ["laptop", "bottle"]},
                           "gym": {"aliases": [], "items": ["towel", "bottle"]}})
tray4.cal.update(state="ok", updated=t(2026, 11, 11, 21, 0), events=[
    {"uid": "g1", "title": "Gym", "start": t(2026, 11, 11, 20), "end": t(2026, 11, 11, 21), "bring": [], "travel": 0},
    {"uid": "w", "title": "Work", "start": t(2026, 11, 12, 9), "end": t(2026, 11, 12, 17), "bring": [], "travel": 0},
    {"uid": "g2", "title": "Gym", "start": t(2026, 11, 12, 19), "end": t(2026, 11, 12, 20), "bring": [], "travel": 0},
])

def at(iso):
    """Simulate a time the way the service does: TRAY_FAKE_NOW, read by set_fake_now()."""
    os.environ["TRAY_FAKE_NOW"] = iso
    tray4.set_fake_now(os.environ["TRAY_FAKE_NOW"])
    return round(tray4.clock())

def reset(bag=None):
    sent.clear()
    tray4.shown.clear(); tray4.removed_at.clear()
    tray4.evening.update(armed=None, changed=0.0, done=None)
    tray4.data.pop("bag", None)
    if bag:
        tray4.data["bag"] = bag
    for s in tray4.SLOTS:
        tray4.last_seen[s], tray4.last_uid[s] = 0.0, None

def tray(now, *uids):
    """What the readers see at `now`, then one pass of the reader loop's tray_changed()."""
    for s in tray4.SLOTS:
        tray4.last_seen[s] = 0.0
    for s, u in zip(tray4.SLOTS, uids):
        tray4.last_uid[s], tray4.last_seen[s] = u, now
    tray4.tray_changed(dict(tray4.tray_tags(now)), now)
    for th in threading.enumerate():   # wait for the check / lift threads
        if th is not threading.current_thread() and th.daemon:
            th.join(2)

def show(name, now):
    s, st = tray4.status(now), tray4.state(now)
    clock = datetime.fromtimestamp(now, VIE).strftime("%a %d/%m %H:%M:%S")
    print(f"  {clock}  plan_for={s['plan_for']:8} LED/status={s['state']:7} missing={s['missing']}"
          f"  next={s['next_event'] and s['next_event']['title']}")
    print(f"  notifications: {sent or 'none'}")
    assert st["plan_for"] == s["plan_for"] and st["simulated"] == os.environ["TRAY_FAKE_NOW"]
    return s, st

# ---------- (a) evening, everything present: silent, LED READY, "Tomorrow: ready" ----------
print("(a) Wed 21:30, bag + laptop + bottle + towel put on the tray")
now = at("2026-11-11T21:30:00")
reset(bag="B")
tray(now)
tray(now + 1, "B", "L", "O", "T")          # bag down: check armed (BAG_SETTLE = 3 s)
tray(now + 3.9, "B", "L", "O", "T")
assert sent == []                          # not settled yet
tray(now + 4.1, "B", "L", "O", "T")        # settled: check runs
s, st = show("a", now + 4.1)
assert sent == [] and s["state"] == "READY" and s["plan_for"] == "tomorrow"
assert s["next_event"]["title"] == "Work"
assert [i["status"] for i in st["tomorrow"]["items"]] == ["ok", "ok", "ok"]   # page: "Tomorrow: ready"

# ---------- (b) evening, one missing: one notification ----------
print("(b) Wed 21:30, bag + laptop + bottle, towel forgotten")
reset(bag="B")
tray(now)
tray(now + 1, "B", "L", "O")
tray(now + 4.1, "B", "L", "O")
s, _ = show("b", now + 4.1)
assert sent == ["Tomorrow: Work 09:00, Gym 19:00. On the tray: laptop, bottle. Missing: towel."], sent
assert s["state"] == "MISSING" and s["missing"] == ["towel"]
tray(now + 30, "B", "L", "O")              # nothing new: no second notification
assert len(sent) == 1

print("(b') same, no bag tag set: first things placed, check after 7 s of quiet, once per evening")
reset()
tray(now)
tray(now + 1, "L", "O")                    # placed at +1 s: EVENING_SETTLE = 7 s -> check at +8 s
tray(now + 7.9, "L", "O")
print(f"  +7.9 s notifications: {sent or 'none'}")
assert sent == []
tray(now + 8.1, "L", "O")
print(f"  +8.1 s notifications: {sent}")
assert sent == ["Tomorrow: Work 09:00, Gym 19:00. On the tray: laptop, bottle. Missing: towel."], sent
tray(now + 40, "L", "O", "T"); tray(now + 70, "L", "O", "T")
assert len(sent) == 1                      # the evening's check is done
reset()
tray(now); tray(now + 1, "K"); tray(now + 3)          # placed and taken off before settling:
tray(now + 30); assert sent == [] and tray4.evening["done"] is None   # no check, still armed for later
reset()
tray(now); tray(now + 1, "L"); tray(now + 5, "L", "O")   # another thing at +5 s: the 7 s start again from there
tray(now + 11.9, "L", "O"); assert sent == []
tray(now + 12.1, "L", "O"); assert len(sent) == 1, sent

# ---------- (c) evening, nothing tomorrow ----------
print("(c) Thu 21:30, nothing planned on Friday")
now = at("2026-11-12T21:30:00")
reset(bag="B")
tray(now); tray(now + 1, "B", "L"); tray(now + 4.1, "B", "L")
s, st = show("c", now + 4.1)
assert sent == [] and s["state"] == "IDLE" and s["plan_for"] == "tomorrow" and s["next_event"] is None
assert st["tomorrow"] == {"keys": [], "items": []}

# ---------- bag lifted in the evening: final reminder only if a today event hasn't started ----------
print("(lift) Thu 21:30 lift (today's Gym 19:00 already started) vs Thu 18:30 lift (Gym 19:00 still ahead)")
reset()
tray(now, "L", "O"); tray(now + 0.5)                   # whole bag gone at once
assert sent == [], sent
now = at("2026-11-12T18:30:00")
reset()
tray(now, "L", "O"); tray(now + 0.5)
print(f"  18:30 notifications: {sent}")
assert sent == ["Bag lifted without everything (Gym 19:00): missing towel"], sent

# ---------- (d) 02:59 vs 03:01, and the switch happening on a running clock ----------
print("(d) Thu 02:59 vs 03:01 (the evening started on Wed)")
now = at("2026-11-12T02:59:00")
reset()
s, _ = show("d1", now)
assert s["plan_for"] == "tomorrow" and s["next_event"]["title"] == "Work"   # "tomorrow" = Thursday
now = at("2026-11-12T03:01:00")
s, _ = show("d2", now)
assert s["plan_for"] == "today" and s["next_event"]["title"] == "Work"
assert tray4.focus_day(at("2026-11-11T17:59:00")) == ("today", datetime(2026, 11, 11).date())
assert tray4.focus_day(at("2026-11-11T18:00:00")) == ("tomorrow", datetime(2026, 11, 12).date())
print("(d') clock started at 02:59:58 and left running: switches by itself, no restart")
at("2026-11-12T02:59:58")
before = tray4.status(tray4.clock())["plan_for"]
time.sleep(2.5)
after = tray4.status(tray4.clock())["plan_for"]
print(f"  {before} -> {after}")
assert (before, after) == ("tomorrow", "today")

tray4.config.update(evening_from="bad", evening_until="25:00")   # broken config: defaults 18:00 / 03:00
assert tray4.focus_day(at("2026-11-11T18:00:00"))[0] == "tomorrow" and tray4.focus_day(at("2026-11-12T03:00:00"))[0] == "today"
# ---------- Demo sheet: simulated time from the page, at once, in memory only ----------
tray4.evening.update(armed="first", changed=5.0, done="x")
tray4.cal["next_fetch"] = 1e12
r = tray4.api("/api/demo_time", {"fake_now": "2026-11-19T19:00"})
assert r["ok"] and tray4.fake["at"] == "2026-11-19T19:00" and tray4.focus_day(tray4.clock())[0] == "tomorrow", r
assert tray4.evening == {"armed": None, "settle": tray4.evening["settle"], "changed": 0.0, "done": None}
assert tray4.cal["next_fetch"] == 0                               # calendar reloaded around the new time
assert not tray4.api("/api/demo_time", {"fake_now": "tomorrow-ish"})["ok"] and tray4.fake["at"] == "2026-11-19T19:00"
assert tray4.api("/api/demo_time", {"fake_now": ""})["ok"] and tray4.fake == {"offset": 0.0, "at": ""}
assert abs(tray4.clock() - time.time()) < 1
# clock moved back with things on the tray: they must not stick (the bug seen on the Pi on 12 Nov)
tray4.api("/api/demo_time", {"fake_now": "2026-11-12T15:28:00"})
reset()
on = tray4.clock()
tray(on, "L", "O", "T")
assert len(tray4.tray_tags(on)) == 3
tray4.api("/api/demo_time", {"fake_now": "2026-11-12T07:30:00"})   # 8 h back, towel then taken off
back = tray4.clock()
assert tray4.tray_tags(back) == [], tray4.tray_tags(back)          # was: all three "on the tray" for 8 h
assert tray4.removed_at == {}
tray(back + 0.1, "L", "O")                                         # what's really there is read again at once
assert sorted(n for _, n in tray4.tray_tags(back + 0.1)) == ["bottle", "laptop"]
tray4.api("/api/demo_time", {"fake_now": ""})
print("demo time: OK")
print("OK")
