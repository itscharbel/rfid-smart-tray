#!/usr/bin/env python3
"""Measurement session for the RFID tray (runs on the Pi, next to tray4.py and scenarios.json).

  python3 test_session.py placement [--trials 10] [--slots R1,R2,R3] [--tags laptop,keys]   G1
  python3 test_session.py scenarios [--only S01,S07]                                       G2 + G3
  python3 test_session.py report          rebuild ~/tray_results/results.md + results.csv
  python3 test_session.py --restore       undo a session that was killed hard (fake_now, stale, weather, bag)
  ~/rfid-env/bin/python test_session.py --dry-run    validate scenarios.json, simulate, no hardware
  python3 test_session.py --write-events-md          regenerate test_events.md from scenarios.json

Never prints or saves tray_config.json values: everything shown goes through say(), which hides them.
"""
import argparse, csv, json, os, signal, statistics, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIOS = os.path.join(HERE, "scenarios.json")
EVENTS_MD = os.path.join(HERE, "test_events.md")
CONFIG = os.environ.get("TRAY_CONFIG", "/home/pi/tray_config.json")
EVENTS = "/home/pi/events.csv"
RESULTS = os.path.expanduser("~/tray_results")
RESTORE = os.path.expanduser("~/.tray_session_restore.json")  # no secrets: the SAVED settings + bag
SAVED = ("stale_after_hours", "weather_override", "weather_stale_hours")  # changed per scenario, put back after
BASE = "http://127.0.0.1:8000"
TRAY_PROCESS = "python -u /home/pi/tray4.py"
SLOTS = ("R1", "R2", "R3")      # the prototype's 3 readers, one tag each
READ_WAIT = 3.0                 # s after Enter for a placement to be read (G1)
STATUS_WAIT = 10.0              # s for the status to show the expected state (G3 target)
NOTIFY_WAIT = 15.0              # s to wait for an expected notification; silence is checked for 12 s
SILENCE_WAIT = 12.0
STATES = {"READY", "MISSING", "IDLE", "STALE"}
OVERRIDES = ("none", "rain", "hot", "cold", "off")
VIE = ZoneInfo("Europe/Vienna")
ABSENT = "__absent__"

HIDE = []  # secret values from the config: never shown

def load_secrets():
    try:
        with open(CONFIG) as f:
            c = json.load(f)
    except (OSError, ValueError):
        return {}
    HIDE[:] = [str(c[k]) for k in ("api_token", "ntfy_topic", "ics_url") if c.get(k)]
    return c

def clean(text):
    text = str(text)
    for v in HIDE:
        text = text.replace(v, "<hidden>")
    return text

def say(*parts):
    print(clean(" ".join(str(p) for p in parts)), flush=True)

class Quit(Exception):
    pass

def ask(prompt):
    """Enter = go on, 's' = skip this one, 'q' = stop (settings are restored)."""
    a = input(clean(prompt) + " ").strip().lower()
    if a == "q":
        raise Quit()
    return a

# ---------- scenarios.json ----------

def validate(spec):
    """-> list of problems. Enforces the 3-reader limit: bag + at most 2, no bag: at most 3."""
    err = []
    tags, events = set(spec.get("tags", [])), spec.get("events", {})
    bag = spec.get("bag_tag")
    if bag not in tags:
        err.append(f"bag_tag {bag!r} is not in 'tags'")
    for k, e in events.items():
        for f in ("title", "date", "start", "end"):
            if not e.get(f):
                err.append(f"event {k}: missing {f}")
        try:
            datetime.fromisoformat(f'{e["date"]}T{e["start"]}'), datetime.fromisoformat(f'{e["date"]}T{e["end"]}')
        except (KeyError, ValueError):
            err.append(f"event {k}: bad date/start/end")
    seen = set()
    for i, s in enumerate(spec.get("scenarios", [])):
        sid = s.get("id", f"#{i + 1}")
        if sid in seen:
            err.append(f"{sid}: duplicate id")
        seen.add(sid)
        for f in ("name", "mode", "event_keys", "items_on_tray", "expected_state", "expected_missing", "expected_notification"):
            if f not in s:
                err.append(f"{sid}: missing {f}")
        items = s.get("items_on_tray", [])
        if s.get("mode") == "bag":
            if len(items) > 2:
                err.append(f"{sid}: bag mode allows at most 2 other items (3 readers: one is the bag), got {len(items)}")
            if bag in items:
                err.append(f"{sid}: list the bag tag only via mode 'bag', not in items_on_tray")
        elif s.get("mode") == "no-bag":
            if len(items) > 3:
                err.append(f"{sid}: at most 3 items (3 readers), got {len(items)}")
        else:
            err.append(f"{sid}: mode must be 'bag' or 'no-bag'")
        if len(set(items)) != len(items):
            err.append(f"{sid}: an item is listed twice")
        for n in items:
            if n not in tags:
                err.append(f"{sid}: item {n!r} is not in 'tags'")
        for k in s.get("event_keys", []):
            if k not in events:
                err.append(f"{sid}: unknown event key {k!r}")
        if s.get("expected_state") not in STATES:
            err.append(f"{sid}: expected_state must be one of {sorted(STATES)}")
        if not isinstance(s.get("expected_missing"), list) or not isinstance(s.get("expected_notification"), bool):
            err.append(f"{sid}: expected_missing must be a list, expected_notification true/false")
        if s.get("fake_now") is not None:
            try:
                datetime.fromisoformat(s["fake_now"])
            except (TypeError, ValueError):
                err.append(f"{sid}: fake_now is not an ISO time")
        if s.get("action", "none") not in ("none", "leave", "lift"):
            err.append(f"{sid}: action must be none, leave or lift")
        if s.get("action") == "lift" and len(items) + (s.get("mode") == "bag") < 2:
            err.append(f"{sid}: a lift needs at least 2 tags on the tray")
        for k in ("stale_after_hours", "weather_stale_hours"):
            if k in s and not isinstance(s[k], (int, float)):
                err.append(f"{sid}: {k} must be a number")
        if s.get("weather_override", "off") not in OVERRIDES:
            err.append(f"{sid}: weather_override must be one of {', '.join(OVERRIDES)}")
        rem = s.get("expected_reminders", [])
        if not isinstance(rem, list):
            err.append(f"{sid}: expected_reminders must be a list")
        elif any(n in s.get("items_on_tray", []) for n in rem):
            err.append(f"{sid}: a reminder is only for things not on the tray")
        if s.get("expected_weather") not in (None, "unavailable"):
            err.append(f"{sid}: expected_weather can only be 'unavailable'")
    if not seen:
        err.append("no scenarios")
    return err

def load_spec():
    with open(SCENARIOS) as f:
        spec = json.load(f)
    err = validate(spec)
    if err:
        say("scenarios.json is invalid, refusing to run:")
        for e in err:
            say("  -", e)
        sys.exit(2)
    return spec

def events_md(spec):
    """test_events.md, generated from scenarios.json so the two can't drift apart."""
    used = {}
    for s in spec["scenarios"]:
        for k in s["event_keys"]:
            used.setdefault(k, []).append(s["id"])
    out = ["# Test calendar events (generated from scenarios.json, do not edit by hand)", "",
           "Create these in the Google Calendar the tray reads, time zone **Europe/Vienna**, as normal timed",
           "events (not all-day). Copy the description exactly, including `Porta:`.", "",
           "The session runs on **Tuesday 2026-11-17**. Each scenario simulates its own time (`fake_now`), so the",
           "events are spread over **Tue 17 to Sat 21 Nov**: that way each scenario sees exactly its events.",
           "**Those days must have no other events** in that calendar: the session checks it before every",
           "scenario and marks a scenario as a setup error if the tray sees anything else that day.", ""]
    for date in sorted({e["date"] for e in spec["events"].values()}):
        d = datetime.fromisoformat(date)
        out += [f"## {d.strftime('%A %d %B %Y')}", "", "| key | title | start | end | description | used by |",
                "|---|---|---|---|---|---|"]
        for k, e in sorted(spec["events"].items(), key=lambda kv: kv[1]["start"]):
            if e["date"] == date:
                desc = f'`{e["description"]}`' if e["description"] else "(empty)"
                out.append(f'| {k} | {e["title"]} | {e["start"]} | {e["end"]} | {desc} | {", ".join(used.get(k, []))} |')
        out.append("")
    days = {datetime.fromisoformat(s["fake_now"]).date().isoformat() for s in spec["scenarios"] if s.get("fake_now")}
    empty = sorted(days - {e["date"] for e in spec["events"].values()})
    if empty:
        out += ["Days that must stay **empty** (no events at all): " + ", ".join(empty), ""]
    out += ["## Also needed on the tray", "",
            "- Tags named exactly: " + ", ".join(f"`{t}`" for t in spec["tags"]) + f" (`{spec['bag_tag']}` is a tag on your bag).",
            "- **No** tag named " + ", ".join(f"`{t}`" for t in spec.get("untagged", [])) + " (S09 tests an untagged object; keep the weather items untagged too, so W1-W3 stay predictable).",
            "- Lists (presets) exactly: " + "; ".join(f"`{n}`: {', '.join(v)}" for n, v in spec["presets"].items())
            + ". No other list may match these event titles.", ""]
    return "\n".join(out)

# ---------- talking to the tray (localhost only) ----------

def http(path, body=None, token=None, timeout=4):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def wait_api(timeout=40, simulated=ABSENT):
    """Until the tray answers, its calendar is loaded and (if given) its simulated clock is as expected."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = http("/api/state")
            if st["cal"]["state"] == "ok" and (simulated == ABSENT or st.get("simulated") == simulated):
                return st
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.5)
    return None

def active_since():
    return subprocess.run(["systemctl", "show", "tray", "-p", "ActiveEnterTimestampMonotonic", "--value"],
                          capture_output=True, text=True).stdout.strip()

def restart_tray(simulated):
    """Clean stop (SIGINT: LEDs reset), systemd brings it back (Restart=always), wait until ready."""
    before = active_since()
    subprocess.run(["pkill", "-INT", "-f", TRAY_PROCESS])
    end = time.time() + 25
    while active_since() == before and time.time() < end:
        time.sleep(0.3)
    return wait_api(simulated=simulated)

def config_set(changes):
    """Atomic edit of tray_config.json, mode 600. Value None = remove the key. Prints nothing."""
    with open(CONFIG) as f:
        c = json.load(f)
    for k, v in changes.items():
        if v is None:
            c.pop(k, None)
        else:
            c[k] = v
    tmp = CONFIG + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(c, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)

def preflight(cfg):
    """Every run: refuse to start if the tray is (or would be) on a simulated clock by accident."""
    problems = []
    if os.path.exists(RESTORE):
        problems.append("a previous session did not finish cleanly. Run: python3 test_session.py --restore")
    if cfg.get("fake_now"):
        problems.append("fake_now is set in tray_config.json. Remove it (or run --restore) before testing")
    if os.environ.get("TRAY_FAKE_NOW"):
        problems.append("TRAY_FAKE_NOW is set in this shell. Run: unset TRAY_FAKE_NOW")
    env = subprocess.run(["systemctl", "show", "tray", "-p", "Environment", "--value"], capture_output=True, text=True).stdout
    if "TRAY_FAKE_NOW" in env:
        problems.append("the tray service has TRAY_FAKE_NOW in its environment (systemd unit / drop-in): remove it")
    try:
        if http("/api/state").get("simulated"):
            problems.append("the running tray uses simulated time: run --restore (it restarts the tray)")
    except (OSError, ValueError):
        problems.append(f"the tray doesn't answer on {BASE}: is the service running? (systemctl status tray)")
    if problems:
        say("FAIL, not starting:")
        for p in problems:
            say("  -", p)
        sys.exit(3)

def restore():
    """Undo what a scenario session changed: fake_now, the SAVED settings, the bag tag; restart if needed."""
    saved = {}
    if os.path.exists(RESTORE):
        with open(RESTORE) as f:
            saved = json.load(f)
    with open(CONFIG) as f:
        c = json.load(f)
    changes = {"fake_now": None}
    for k in SAVED:
        if k in saved:
            changes[k] = None if saved[k] == ABSENT else saved[k]
    need = bool(c.get("fake_now")) or any(k in saved and c.get(k, ABSENT) != saved[k] for k in SAVED)
    try:
        sim = http("/api/state").get("simulated")
    except (OSError, ValueError):
        sim = None
    if need or sim:
        config_set(changes)
        say("Restoring the real clock and settings, restarting the tray…")
        say("  tray back on the real clock." if restart_tray(simulated=None) else
            "  the tray didn't come back within 40 s: check `systemctl status tray`.")
    if "bag" in saved:
        try:
            http("/api/bag", {"uid": saved["bag"] or ""})
        except (OSError, ValueError):
            say("  could not restore the bag tag (tray not answering); set it again on the Things tab.")
    if os.path.exists(RESTORE):
        os.remove(RESTORE)
    say("Restore done.")

# ---------- events.csv ----------

def read_events(since):
    rows = []
    for path in (EVENTS + ".1", EVENTS):
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        t = datetime.fromisoformat(r["ts"]).timestamp()
                    except (KeyError, ValueError):
                        continue
                    if t >= since:
                        rows.append({**r, "t": t})
        except OSError:
            pass
    return sorted(rows, key=lambda r: r["t"])

def latencies(rows, last_enter, trigger=None):
    """-> (status_s, notify_s, note). status: last slot change (up to the last Enter, plus the removal
    timeout) -> first status change after it. notify: from that change, or from `trigger` (the
    'I'm leaving' press) when the notification is caused by it."""
    slot = [r["t"] for r in rows if r["type"] == "slot" and r["t"] <= last_enter + 4]
    if not slot:
        return None, None, "no slot change logged"
    last = max(slot)
    st = next((r["t"] for r in rows if r["type"] == "state" and r["t"] > last), None)
    base = trigger or last
    nt = next((r["t"] for r in rows if r["type"] == "notify" and r["t"] >= base), None)
    return (round(st - last, 2) if st else None), (round(nt - base, 2) if nt else None), \
        "" if st else "status did not change after the last placement (it already showed this)"

# ---------- comparison (shared by the real run and --dry-run) ----------

def compare(sc, status, notified):
    """-> (state_ok, missing_ok, notification_ok, weather_ok, reasons). Missing = missing + no_tag, order ignored.
    weather_ok: the reminders (weather items without a tag) and, if expected, "weather unavailable"."""
    got = sorted(status.get("missing", []) + status.get("no_tag", []))
    want = sorted(sc["expected_missing"])
    reasons = []
    st_ok = status.get("state") == sc["expected_state"]
    if not st_ok:
        reasons.append(f'state {status.get("state")} instead of {sc["expected_state"]}')
    mi_ok = got == want
    if not mi_ok:
        reasons.append(f"missing {got} instead of {want}")
    no_ok = bool(notified) == sc["expected_notification"]
    if not no_ok:
        reasons.append(("notification sent: " + notified[0]) if notified else "no notification, one was expected")
    rem_want = sorted(sc.get("expected_reminders", []))
    if no_ok and notified and rem_want and not ("Remember:" in notified[0] and all(n in notified[0] for n in rem_want)):
        no_ok = False
        reasons.append("notification without the weather reminder: " + notified[0])
    if no_ok and notified and not sc["expected_missing"] and "missing" in notified[0].lower():
        no_ok = False  # e.g. the evening check ran before everything was down
        reasons.append("notification says something is missing: " + notified[0])
    rem_got = sorted(r["name"] for r in status.get("reminders", []))
    wx_ok = rem_got == rem_want
    if not wx_ok:
        reasons.append(f"reminders {rem_got} instead of {rem_want}")
    if sc.get("expected_weather") == "unavailable" and not status.get("weather_unavailable"):
        wx_ok = False
        reasons.append("weather not reported as unavailable")
    return st_ok, mi_ok, no_ok, wx_ok, reasons

def setup_check(spec, sc, st, lead_minutes):
    """The tray must see exactly the scenario's test events on the day in focus, and no reminder may
    fall inside the scenario. -> list of problems (setup errors, not tray failures)."""
    probs = []
    if sc.get("fake_now") and st.get("simulated") != sc["fake_now"]:
        probs.append("the tray is not on the scenario's simulated time")
    day = st.get("focus_date")
    seen = {}
    for e in st.get("day", []) + st.get("events", []):
        d = datetime.fromtimestamp(e["start_ts"], VIE)
        if d.date().isoformat() == day:
            seen[e["key"]] = (e["title"].strip().lower(), d.strftime("%H:%M"))  # case doesn't matter, as in the tray
    got = sorted(set(seen.values()))
    want = sorted((spec["events"][k]["title"].lower(), spec["events"][k]["start"]) for k in sc["event_keys"])
    if got != want:
        extra = [f"{t} {h}" for t, h in got if (t, h) not in want]
        lack = [f"{t} {h}" for t, h in want if (t, h) not in got]
        probs.append(f"calendar on {day}: " + "; ".join(
            ([f"unexpected {', '.join(extra)}"] if extra else []) + ([f"not found {', '.join(lack)}"] if lack else [])))
    if sc.get("fake_now"):
        start = datetime.fromisoformat(sc["fake_now"]).replace(tzinfo=VIE).timestamp()
        for e in spec["events"].values():
            ev = datetime.fromisoformat(f'{e["date"]}T{e["start"]}').replace(tzinfo=VIE).timestamp()
            if start <= ev - lead_minutes * 60 < start + 1800:
                probs.append(f'the morning reminder for {e["title"]} would fire during the scenario (lead_minutes {lead_minutes})')
    return probs

# ---------- G1: placement ----------

def wait_slot(name, slot, token):
    end, last = time.time() + READ_WAIT, "nothing read"
    while time.time() < end:
        items = {i["name"]: i for i in http("/api/v1/items", token=token)["items"]}
        it = items.get(name)
        if it and it["on_tray"] and it["slot"] == slot:
            return True, ""
        others = [f'{n} on {i["slot"]}' for n, i in items.items() if i["on_tray"] and n != name]
        last = (f'read on {it["slot"]} instead of {slot}' if it and it["on_tray"] else
                f'other tag read: {", ".join(others)}' if others else
                "an unnamed tag was read" if http("/api/state")["on_tray"] else "nothing read")
        time.sleep(0.2)
    return False, last

def wait_empty():
    while True:
        end = time.time() + 4
        while time.time() < end:
            on = http("/api/state")["on_tray"]
            if not on:
                return
            time.sleep(0.3)
        ask(f'Still on the tray: {", ".join(t["name"] or "unnamed tag" for t in on)}. Clear the tray, then press Enter.')

def placement(args, cfg):
    token = cfg.get("api_token")
    if not token:
        say("FAIL: no api_token in tray_config.json (the tray creates one when it starts)."); sys.exit(3)
    names = args.tags.split(",") if args.tags else sorted(t["name"] for t in http("/api/state")["tags"])
    slots = args.slots.split(",")
    total, k = len(slots) * len(names) * args.trials, 0
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "placement_trials.csv")
    say(f"Placement test (G1): {len(slots)} slots × {len(names)} tags × {args.trials} trials = {total}.")
    say("At each prompt: Enter = done, s = skip, q = stop. Results are saved after every trial.")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "slot", "tag", "trial", "result", "reason"])
        wait_empty()
        for slot in slots:
            for name in names:
                for trial in range(1, args.trials + 1):
                    k += 1
                    if ask(f"[{k}/{total}] Place {name} on slot {slot[1:]} ({slot}), then press Enter.") == "s":
                        w.writerow([datetime.now(VIE).isoformat(timespec="seconds"), slot, name, trial, "skipped", ""])
                        continue
                    ok, why = wait_slot(name, slot, token)
                    say("   pass" if ok else f"   FAIL: {why}")
                    w.writerow([datetime.now(VIE).isoformat(timespec="seconds"), slot, name, trial,
                                "pass" if ok else "fail", why])
                    f.flush()
                    ask("   Remove it, then press Enter.")
                    wait_empty()
    write_report()

# ---------- G2 + G3: scenarios ----------

def run_scenario(spec, sc, n, of, uid_of, cfg):
    r = {"id": sc["id"], "name": sc["name"], "result": "fail", "state_expected": sc["expected_state"],
         "missing_expected": "|".join(sc["expected_missing"]), "notification_expected": sc["expected_notification"],
         "reminders_expected": "|".join(sorted(sc.get("expected_reminders", [])))}
    say(f'\n=== Scenario {sc["id"]} ({n}/{of}): {sc["name"]} ===')
    wait_empty()
    bag = uid_of[spec["bag_tag"]] if sc["mode"] == "bag" else ""
    http("/api/bag", {"uid": bag})
    config_set({"fake_now": sc.get("fake_now"),  # never the real forecast: weather "off" unless the scenario says so
                **{k: sc.get(k, cfg.get(k)) for k in SAVED}, "weather_override": sc.get("weather_override", "off")})
    say("Setting up (simulated time, restart)…")
    st = restart_tray(simulated=sc.get("fake_now"))
    if not st:
        return {**r, "result": "setup_error", "reason": "tray not ready with the scenario's settings within 40 s"}
    probs = setup_check(spec, sc, st, int(cfg.get("lead_minutes", 15)))
    if probs:
        say("   SETUP ERROR: " + "; ".join(probs))
        return {**r, "result": "setup_error", "reason": "; ".join(probs)}
    when = datetime.fromisoformat(sc["fake_now"]).strftime("%a %d/%m %H:%M") if sc.get("fake_now") else "real time"
    evs = ", ".join(f'{spec["events"][k]["title"]} {spec["events"][k]["start"]}' for k in sc["event_keys"]) or "none"
    put = ([f'the bag ({spec["bag_tag"]})'] if sc["mode"] == "bag" else []) + sc["items_on_tray"]
    say(f"   Time: {when} · plan for: {st['plan_for']} · events that day: {evs}")
    say(f"   Mode: {'bag tag set' if bag else 'no bag tag'} · weather: {sc.get('weather_override', 'off')}")
    if st.get("plan_for") == "tomorrow":
        say("   Evening: the check runs " + ("3 s after the bag goes down: put the other things first, the bag last."
            if bag else "7 s after the tray stops changing: put everything down together (within 7 s)."))
    t_start = time.time()
    if ask(f"   Put on the tray (any readers): {', '.join(put)}. Then press Enter.") == "s":
        return {**r, "result": "skipped", "reason": "skipped by the tester"}
    t_place = last_enter = time.time()
    end, status = t_place + STATUS_WAIT, {}
    while time.time() < end:  # until it shows the expected state + missing list, at most 10 s
        status = http("/api/v1/status")
        ok = compare(sc, status, [])
        if ok[0] and ok[1] and ok[3]:
            break
        time.sleep(0.25)
    trigger = None
    if sc.get("action") == "leave":
        trigger = time.time()
        http("/api/leave", {})
        say("   (pressed “I’m leaving” through the API)")
    elif sc.get("action") == "lift":
        ask("   Now lift the bag with everything off the tray in one go, then press Enter.")
        last_enter = time.time()
    wait_until = (trigger or last_enter) + (NOTIFY_WAIT if sc["expected_notification"] else SILENCE_WAIT)
    rows = []
    while time.time() < wait_until:
        rows = read_events(t_start)
        if sc["expected_notification"] and any(x["type"] == "notify" for x in rows):
            break
        time.sleep(0.5)
    time.sleep(0.5)
    rows = read_events(t_start)
    notified = [x["detail"] for x in rows if x["type"] == "notify"]
    st_ok, mi_ok, no_ok, wx_ok, reasons = compare(sc, status, notified)
    lat_s, lat_n, note = latencies(rows, last_enter, trigger)
    r.update(result="pass" if st_ok and mi_ok and wx_ok else "fail", state_observed=status.get("state"),
             reminders_observed="|".join(sorted(x["name"] for x in status.get("reminders", []))),
             missing_observed="|".join(sorted(status.get("missing", []) + status.get("no_tag", []))),
             notification_observed=bool(notified), notification_ok=no_ok,
             latency_status_s=lat_s, latency_notification_s=lat_n,
             latency_notification_from="leave press" if trigger else "last placement/removal",
             reason="; ".join(reasons + ([note] if note and sc["expected_state"] == "MISSING" else [])))
    say("   " + ("pass" if r["result"] == "pass" else "FAIL: " + r["reason"])
        + (f" (notification: {'ok' if no_ok else 'WRONG'})" if r["result"] == "pass" else "")
        + f" · status after {lat_s} s · notification after {lat_n} s")
    if sc.get("action") != "lift":
        ask("   Clear the tray, then press Enter.")
    return r

SCENARIO_COLUMNS = ["time", "id", "name", "result", "state_expected", "state_observed", "missing_expected",
                    "missing_observed", "reminders_expected", "reminders_observed", "notification_expected", "notification_observed", "notification_ok",
                    "latency_status_s", "latency_notification_s", "latency_notification_from", "reason"]

def scenarios(args, cfg):
    spec = load_spec()
    pick = [s for s in spec["scenarios"] if not args.only or s["id"] in args.only.split(",")]
    st = http("/api/state")
    tags = {t["name"]: t["uid"] for t in st["tags"]}
    presets = {p["name"]: sorted(p["items"]) for p in http("/api/presets")["presets"]}
    probs = [f"no tag named {t!r}" for t in spec["tags"] if t not in tags] + \
            [f"tag {t!r} must not exist (untagged test)" for t in spec.get("untagged", []) if t in tags] + \
            [f"list {n!r} should be {sorted(v)}" for n, v in spec["presets"].items() if presets.get(n) != sorted(v)]
    if probs:
        say("Tray setup doesn't match scenarios.json, not starting:")
        for p in probs:
            say("  -", p)
        sys.exit(3)
    with open(RESTORE, "w") as f:  # what to put back, even after a hard kill (--restore)
        json.dump({**{k: cfg.get(k, ABSENT) for k in SAVED}, "bag": st.get("bag")}, f)
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "scenario_results.csv")
    ids = {sc["id"] for sc in pick}
    keep = [r for r in read_csv("scenario_results.csv") if r.get("id") not in ids] if args.only else []
    say(f"Scenario test (G2/G3): {len(pick)} scenarios. Enter = done, s = skip, q = stop (settings are restored).")
    if keep:
        say(f"Keeping the earlier results of {len(keep)} other scenarios; {', '.join(sorted(ids))} will be replaced.")
    try:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, SCENARIO_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(keep)  # --only: the other scenarios' results stay
            for i, sc in enumerate(pick, 1):
                r = run_scenario(spec, sc, i, len(pick), tags, cfg)
                w.writerow({k: clean(v) for k, v in {"time": datetime.now(VIE).isoformat(timespec="seconds"), **r}.items()})
                f.flush()
    finally:
        restore()
        write_report()

# ---------- report ----------

def read_csv(name):
    try:
        with open(os.path.join(RESULTS, name), newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []

def pct(a, b):
    return f"{a}/{b} = {100 * a / b:.1f} %" if b else "not run"

def write_report(results=RESULTS):
    global RESULTS
    RESULTS = results
    pl = [r for r in read_csv("placement_trials.csv") if r["result"] in ("pass", "fail")]
    sc = sorted(read_csv("scenario_results.csv"), key=lambda r: r["id"])  # S01..S14, then W1..W4
    run = [r for r in sc if r["result"] in ("pass", "fail")]
    lat = lambda col: [float(r[col]) for r in run if r.get(col) not in (None, "", "None")]
    ls, ln = lat("latency_status_s"), lat("latency_notification_s")
    miss = [r for r in run if r["state_expected"] == "MISSING"]
    no_lat = lambda r: r.get("latency_status_s") in ("", "None", None)
    # passed with no change to time = the right missing list was already showing when the things went down (e.g. S08): 0 s
    shown10 = [r for r in miss if (not no_lat(r) and float(r["latency_status_s"]) <= 10) or (no_lat(r) and r["result"] == "pass")]
    g1 = sum(r["result"] == "pass" for r in pl)
    g2 = sum(r["result"] == "pass" for r in run)
    verdict = lambda ok, n: "not run" if not n else ("MET" if ok else "NOT MET")
    md = ["# RFID tray: test results", "", f"Generated {datetime.now(VIE).strftime('%Y-%m-%d %H:%M')} from "
          "`placement_trials.csv` and `scenario_results.csv` in ~/tray_results.", "",
          "| goal | result | target | verdict |", "|---|---|---|---|",
          f"| G1 placements read correctly | {pct(g1, len(pl))} | ≥ 95 % | {verdict(len(pl) and g1 / len(pl) >= .95, len(pl))} |",
          f"| G2 scenarios with right state + missing list (+ weather reminders) | {pct(g2, len(run))} | ≥ 90 % | {verdict(len(run) and g2 / len(run) >= .9, len(run))} |",
          f"| G3 missing shown within 10 s (MISSING scenarios) | {pct(len(shown10), len(miss))} | all | {verdict(len(shown10) == len(miss), len(miss))} |",
          ""]
    if pl:
        md += ["## G1 by slot and by tag", "", "| slot | passed |", "|---|---|"]
        md += [f"| {s} | {pct(sum(r['result'] == 'pass' for r in pl if r['slot'] == s), sum(r['slot'] == s for r in pl))} |"
               for s in sorted({r["slot"] for r in pl})]
        md += ["", "| tag | passed |", "|---|---|"]
        md += [f"| {t} | {pct(sum(r['result'] == 'pass' for r in pl if r['tag'] == t), sum(r['tag'] == t for r in pl))} |"
               for t in sorted({r["tag"] for r in pl})]
        md.append("")
    if sc:
        setup = [r for r in sc if r["result"] == "setup_error"]
        skipped = [r for r in sc if r["result"] == "skipped"]
        md += ["## G2 / G3 scenarios", "",
               f"Counted: {len(run)}. Setup errors (calendar/tray setup, not counted): {len(setup)}. Skipped: {len(skipped)}.",
               f"Notification right (sent / silent as expected): {pct(sum(r.get('notification_ok') == 'True' for r in run), len(run))}.", "",
               "| latency | n | median | max |", "|---|---|---|---|",
               f"| (a) last placement/removal → status change | {len(ls)} | {statistics.median(ls) if ls else '-'} s | {max(ls) if ls else '-'} s |",
               f"| (b) last placement/removal (or “I’m leaving” press) → notification | {len(ln)} | {statistics.median(ln) if ln else '-'} s | {max(ln) if ln else '-'} s |",
               "", "Removals are logged when the tag times out (up to 2.5 s after it left), so (a) after a lift is conservative.",
               "G3: a MISSING scenario that passed without a status change (the right missing list was already showing when the",
               "things were placed, e.g. S08) counts as shown within 10 s.", "",
               "| id | scenario | result | state exp/got | missing exp/got | reminders exp/got | notif. exp/got | (a) s | (b) s |",
               "|---|---|---|---|---|---|---|---|---|"]
        md += [f'| {r["id"]} | {r["name"]} | {r["result"]} | {r["state_expected"]}/{r.get("state_observed") or "-"} | '
               f'{r["missing_expected"] or "-"}/{r.get("missing_observed") or "-"} | '
               f'{r.get("reminders_expected") or "-"}/{r.get("reminders_observed") or "-"} | '
               f'{r["notification_expected"]}/{r.get("notification_observed") or "-"} | '
               f'{r.get("latency_status_s") or "-"} | {r.get("latency_notification_s") or "-"} |' for r in sc]
        md.append("")
    fails = [("placement", f'{r["slot"]} {r["tag"]} #{r["trial"]}', r["reason"]) for r in pl if r["result"] == "fail"] + \
            [("scenario", f'{r["id"]} {r["name"]}', r["reason"] or "notification wrong")
             for r in sc if r["result"] in ("fail", "setup_error") or r.get("notification_ok") == "False"]
    md += ["## Failures", "", "| part | what | reason |", "|---|---|---|"] + \
          [f"| {p} | {w} | {why.replace('|', '/')} |" for p, w, why in fails] if fails else ["## Failures", "", "None."]
    os.makedirs(results, exist_ok=True)
    with open(os.path.join(results, "results.md"), "w") as f:
        f.write(clean("\n".join(md)) + "\n")
    with open(os.path.join(results, "results.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["part", "id", "what", "result", "reason", "latency_status_s", "latency_notification_s"])
        w.writerows([clean(x) for x in row] for row in
                    [["placement", f'{r["slot"]}-{r["tag"]}-{r["trial"]}', f'{r["tag"]} on {r["slot"]}', r["result"], r["reason"], "", ""] for r in pl] +
                    [["scenario", r["id"], r["name"], r["result"], r["reason"], r.get("latency_status_s", ""),
                      r.get("latency_notification_s", "")] for r in sc])
    say(f"Report: {os.path.join(results, 'results.md')} (and results.csv)")

# ---------- --dry-run: validate + simulate with tray4's own logic, no hardware ----------

ICS_TZ = """BEGIN:VTIMEZONE\nTZID:Europe/Vienna\nBEGIN:DAYLIGHT\nTZOFFSETFROM:+0100\nTZOFFSETTO:+0200\nTZNAME:CEST
DTSTART:19700329T020000\nRRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\nEND:DAYLIGHT\nBEGIN:STANDARD\nTZOFFSETFROM:+0200
TZOFFSETTO:+0100\nTZNAME:CET\nDTSTART:19701025T030000\nRRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\nEND:STANDARD\nEND:VTIMEZONE"""

def ics(spec):
    ev = []
    for k, e in spec["events"].items():
        d, s, t = e["date"].replace("-", ""), e["start"].replace(":", ""), e["end"].replace(":", "")
        ev.append(f"BEGIN:VEVENT\nUID:{k}@tray-test\nSUMMARY:{e['title']}\nDTSTART;TZID=Europe/Vienna:{d}T{s}00\n"
                  f"DTEND;TZID=Europe/Vienna:{d}T{t}00\nDESCRIPTION:{e['description'].replace(',', chr(92) + ',')}\nEND:VEVENT")
    return "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:tray-test\n" + ICS_TZ + "\n" + "\n".join(ev) + "\nEND:VCALENDAR\n"

def dry_run():
    say("Dry run: validating scenarios.json…")
    spec = load_spec()
    say(f"  {len(spec['scenarios'])} scenarios, {len(spec['events'])} events: valid (3-reader limit respected).")
    bad = dict(spec, scenarios=[dict(spec["scenarios"][0], mode="bag", items_on_tray=["laptop", "bottle", "towel"])])
    assert any("at most 2" in e for e in validate(bad)), "the 3-reader check did not trip"
    say("  an invalid file (bag + 3 items) is refused: ok")
    if os.path.exists(EVENTS_MD):
        say("  test_events.md is " + ("in sync with scenarios.json" if open(EVENTS_MD).read() == events_md(spec) + "\n"
                                       else "OUT OF DATE: run --write-events-md"))
    os.environ["TRAY_CONFIG"] = os.path.join(tempfile.mkdtemp(), "tray_config.json")  # never the real one
    os.environ.pop("TRAY_FAKE_NOW", None)
    sys.path.insert(0, HERE)
    try:
        import tray4 as t4
    except ImportError as e:
        say(f"  cannot load tray4 ({e}): run with the venv, e.g. ~/rfid-env/bin/python test_session.py --dry-run")
        sys.exit(2)
    sent = []
    t4.notify = lambda msg, click=None: sent.append(msg) or True
    t4.save = t4.save_config = lambda: None
    t4.log = lambda msg: None
    t4.urllib.request.urlopen = None  # no network
    uid = {n: f"U{i}" for i, n in enumerate(spec["tags"])}

    def tick(now, names):
        for i, s in enumerate(t4.SLOTS):
            on = i < len(names)
            t4.last_uid[s], t4.last_seen[s] = (uid[names[i]], now) if on else (None, 0.0)
        t4.tray_changed(dict(t4.tray_tags(now)), now)
        for th in threading.enumerate():
            if th is not threading.current_thread() and th.daemon:
                th.join(2)

    sim = []
    say("\n  Simulated with tray4's own logic (placement 60 s after fake_now, read 9 s later):")
    for sc in spec["scenarios"]:
        start = datetime.fromisoformat(sc["fake_now"]).replace(tzinfo=VIE).timestamp()
        t4.data.update(tags={uid[n]: n for n in spec["tags"]}, unnamed={}, notified={}, event_overrides={},
                       presets={n: {"aliases": [], "items": list(v)} for n, v in spec["presets"].items()})
        t4.data.pop("bag", None)
        if sc["mode"] == "bag":
            t4.data["bag"] = uid[spec["bag_tag"]]
        t4.config.clear()
        t4.config.update(ics_url="https://example.invalid/test.ics", lead_minutes=15,
                         weather_override=sc.get("weather_override", "off"))
        for k in ("stale_after_hours", "weather_stale_hours"):
            if k in sc:
                t4.config[k] = sc[k]
        t4.rules[:] = [dict(r) for r in t4.DEFAULT_RULES]
        t4.weather.update(hours=[], fetched_at=None)  # no forecast: only simulated weather can apply
        t4.reminded.clear()
        t4.cal.update(state="ok", updated=start, events=t4.parse_ics(ics(spec).encode(), t4.start_of_day(start), start + 7 * 86400))
        t4.shown.clear(); t4.removed_at.clear(); t4.evening.update(armed=None, changed=0.0, done=None)
        sent.clear()
        on = ([spec["bag_tag"]] if sc["mode"] == "bag" else []) + sc["items_on_tray"]
        t0 = start + 60
        t4.set_fake_now(sc["fake_now"]); t4.fake["offset"] = 0.0          # 'simulated' shows in state(), clock untouched
        tick(t0 - 1, []); tick(t0, on); tick(t0 + 9, on)
        status = t4.status(t0 + 9)
        state = t4.state(t0 + 9)
        if sc.get("action") == "leave":
            t4.leaving_check(t0 + 10)
        elif sc.get("action") == "lift":
            tick(t0 + 10, on); tick(t0 + 11, [])
        probs = setup_check(spec, sc, state, 15)
        ok = compare(sc, status, list(sent))
        good = all(ok[:4]) and not probs
        sim.append(good)
        say(f'  {sc["id"]} {"ok  " if good else "MISMATCH"} {status["state"]:7} plan_for={status["plan_for"]:8} '
            f'missing={sorted(status["missing"] + status["no_tag"])} notification={"yes" if sent else "no"}'
            + (f' reminders={[x["name"] for x in status["reminders"]]}' if status["reminders"] else "")
            + (" weather=unavailable" if status["weather_unavailable"] else "")
            + ("" if good else "  <- " + "; ".join(ok[4] + probs)))
        if sent:
            say(f'        “{sent[0]}”')
    t4.set_fake_now("")

    say("\n  Comparison logic self-test:")
    sc = spec["scenarios"][0]
    assert compare(sc, {"state": sc["expected_state"], "missing": sc["expected_missing"], "no_tag": []}, [])[:3] == (True, True, True)
    wrong = compare(sc, {"state": "MISSING", "missing": ["towel"], "no_tag": []}, ["Don't leave yet"])
    assert wrong[:3] == (False, False, False), wrong
    w1 = next(x for x in spec["scenarios"] if x.get("expected_reminders"))
    good_w = {"state": w1["expected_state"], "missing": w1["expected_missing"], "no_tag": [],
              "reminders": [{"name": n, "reason": "x"} for n in w1["expected_reminders"]]}
    assert compare(w1, good_w, ["Remember: " + ", ".join(w1["expected_reminders"])] if w1["expected_notification"] else [])[:4] == (True,) * 4
    assert not compare(w1, dict(good_w, reminders=[]), [])[3]
    assert not compare(w1, good_w, ["Tomorrow: Work 09:00. On the tray: bottle. Missing: laptop. Remember: umbrella"])[2]
    say("    right answer passes; wrong state / missing list / notification / reminders each fail: ok (" + "; ".join(wrong[4]) + ")")
    rows = [{"type": "slot", "t": 100.0}, {"type": "slot", "t": 101.2}, {"type": "state", "t": 101.9},
            {"type": "notify", "t": 104.2}, {"type": "state", "t": 110.0}]
    assert latencies(rows, 102.0) == (0.7, 3.0, ""), latencies(rows, 102.0)
    assert latencies(rows, 102.0, trigger=103.0)[1] == 1.2
    say("    latency from events.csv rows: status 0.7 s, notification 3.0 s (1.2 s from a leave press): ok")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "scenario_results.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, SCENARIO_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerow({"id": "S07", "name": "demo", "result": "pass", "state_expected": "MISSING", "state_observed": "MISSING",
                        "notification_ok": True, "latency_status_s": 0.7, "latency_notification_s": 1.2})
        write_report(d)
        assert "G2 scenarios" in open(os.path.join(d, "results.md")).read()
    say("    report writer: ok")
    if not all(sim):
        say(f"\nDRY RUN FAILED: {sim.count(False)} scenario(s) don't match their expected values."); sys.exit(1)
    say(f"\nDry run OK: all {len(sim)} scenarios give their expected state, missing list and notification.")

def main():
    ap = argparse.ArgumentParser(description="RFID tray measurement session (G1 placement, G2 scenarios, G3 latency)")
    ap.add_argument("part", nargs="?", choices=["placement", "scenarios", "report"])
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--slots", default=",".join(SLOTS))
    ap.add_argument("--tags", help="comma-separated tag names (default: all named tags)")
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--write-events-md", action="store_true")
    a = ap.parse_args()
    if a.write_events_md:
        with open(EVENTS_MD, "w") as f:
            f.write(events_md(load_spec()) + "\n")
        return say(f"Wrote {EVENTS_MD}")
    if a.dry_run:
        return dry_run()
    cfg = load_secrets()
    for sig in (signal.SIGTERM, signal.SIGHUP):  # closed terminal / kill: still restore in finally
        signal.signal(sig, lambda *_: sys.exit(130))
    if a.restore:
        return restore()
    if a.part == "report":
        return write_report()
    if not a.part:
        ap.error("choose placement, scenarios or report (or --dry-run / --restore)")
    preflight(cfg)
    try:
        placement(a, cfg) if a.part == "placement" else scenarios(a, cfg)
    except (Quit, KeyboardInterrupt):
        say("\nStopped. Partial results are saved.")
        write_report()

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # never a raw traceback with values in it
        say(f"Error: {type(e).__name__}: {e}")
        sys.exit(1)
