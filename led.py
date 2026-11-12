"""Tray status on the Pi 3 B+ onboard LEDs: green = ACT, red = PWR.

Writing needs the udev rule in 99-tray-leds.rules (group gpio may write brightness/trigger).
If the LEDs are missing or not writable, set_state() logs once and the tray keeps working.
"""
import os

BASE = "/sys/class/leds"
LEDS = {"green": ("ACT", "mmc0"), "red": ("PWR", "default-on")}  # name, default trigger to restore
# (green, red): "0"/"1" = steady off/on, "timer" = kernel blink, 500 ms on / 500 ms off = 1 Hz
STATES = {"READY": ("1", "0"), "MISSING": ("0", "1"), "IDLE": ("0", "0"), "STALE": ("1", "timer")}

_current = None
_warned = False

def _write(led, attr, value):
    with open(os.path.join(BASE, led, attr), "w") as f:
        f.write(value)

def set_state(state, log=print):
    global _current, _warned
    if state == _current:
        return
    _current = state  # also on failure: don't retry the same state every second
    try:
        for (led, _), value in zip(LEDS.values(), STATES[state]):
            if value == "timer":
                _write(led, "trigger", "timer")
            else:
                _write(led, "trigger", "none")
                _write(led, "brightness", value)
    except OSError as e:
        if not _warned:
            _warned = True
            log(f"Status LEDs unavailable, carrying on without them ({e})")

def restore():
    """Back to the Pi's own behaviour: ACT = SD card activity, PWR = always on."""
    global _current
    _current = None
    for led, trigger in LEDS.values():
        try:
            _write(led, "trigger", trigger)
        except OSError:
            pass
