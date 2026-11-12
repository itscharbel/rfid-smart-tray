# Visual LED check on the Pi. Stop the service first so it doesn't fight over the LEDs:
#   sudo systemctl stop tray; ~/rfid-env/bin/python ~/test_led.py; sudo systemctl start tray
import time, led

SEE = {"READY": "green on, red off", "MISSING": "green off, red on",
       "IDLE": "both off", "STALE": "green on, red blinking 1x per second"}
try:
    for state, what in SEE.items():
        print(f"{state:8} -> {what}", flush=True)
        led.set_state(state)
        time.sleep(2)
finally:
    led.restore()
    print("restored: green = SD activity, red = on")
