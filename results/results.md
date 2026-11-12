# RFID tray: test results

Generated 2026-11-12 17:14 from `placement_trials.csv` and `scenario_results.csv` in ~/tray_results.

| goal | result | target | verdict |
|---|---|---|---|
| G1 placements read correctly | 29/30 = 96.7 % | ≥ 95 % | MET |
| G2 scenarios with right state + missing list (+ weather reminders) | 18/18 = 100.0 % | ≥ 90 % | MET |
| G3 missing shown within 10 s (MISSING scenarios) | 7/7 = 100.0 % | all | MET |

## G1 by slot and by tag

| slot | passed |
|---|---|
| R1 | 9/10 = 90.0 % |
| R2 | 10/10 = 100.0 % |
| R3 | 10/10 = 100.0 % |

| tag | passed |
|---|---|
| bag | 5/6 = 83.3 % |
| bottle | 6/6 = 100.0 % |
| keys | 6/6 = 100.0 % |
| laptop | 6/6 = 100.0 % |
| towel | 6/6 = 100.0 % |

## G2 / G3 scenarios

Counted: 18. Setup errors (calendar/tray setup, not counted): 0. Skipped: 0.
Notification right (sent / silent as expected): 18/18 = 100.0 %.

| latency | n | median | max |
|---|---|---|---|
| (a) last placement/removal → status change | 15 | 0.38 s | 0.88 s |
| (b) last placement/removal (or “I’m leaving” press) → notification | 8 | 0.81 s | 7.99 s |

Removals are logged when the tag times out (up to 2.5 s after it left), so (a) after a lift is conservative.
G3: a MISSING scenario that passed without a status change (the right missing list was already showing when the
things were placed, e.g. S08) counts as shown within 10 s.

| id | scenario | result | state exp/got | missing exp/got | reminders exp/got | notif. exp/got | (a) s | (b) s |
|---|---|---|---|---|---|---|---|---|
| S01 | Gym: 3 items, all present (no bag) | pass | READY/READY | -/- | -/- | False/False | 0.75 | None |
| S02 | Work: laptop and bottle | pass | READY/READY | -/- | -/- | False/False | 0.53 | None |
| S03 | Two events in a row (Work + Gym), keys forgotten | pass | MISSING/MISSING | keys/keys | -/- | True/True | 0.11 | 0.79 |
| S04 | Porta: line only (Dinner: keys, towel) | pass | READY/READY | -/- | -/- | False/False | 0.36 | None |
| S05 | Event with no preset and no Porta: line (nothing to bring) | pass | IDLE/IDLE | -/- | -/- | False/False | None | None |
| S06 | Everything present plus an extra thing: silence | pass | READY/READY | -/- | -/- | False/False | 0.0 | None |
| S07 | One item missing (Work without bottle) | pass | MISSING/MISSING | bottle/bottle | -/- | True/True | 0.71 | 0.77 |
| S08 | All missing (only an unneeded laptop on the tray) | pass | MISSING/MISSING | bottle|towel|keys/bottle|keys|towel | -/- | True/True | None | 0.76 |
| S09 | Untagged object (umbrella has no tag) | pass | MISSING/MISSING | umbrella/umbrella | -/- | True/True | 0.69 | 0.83 |
| S10 | Bag lifted while the bottle is missing | pass | MISSING/MISSING | bottle/bottle | -/- | True/True | 0.38 | 0.69 |
| S11 | Evening, bag put down, bottle missing for tomorrow | pass | MISSING/MISSING | bottle/bottle | -/- | True/True | 0.26 | 3.74 |
| S12 | Evening, everything ready for tomorrow (no bag): silence | pass | READY/READY | -/- | -/- | False/False | 0.79 | None |
| S13 | Stale calendar (stale_after_hours = 0 for this scenario) | pass | STALE/STALE | -/- | -/- | False/False | 0.11 | None |
| S14 | No event at all | pass | IDLE/IDLE | -/- | -/- | False/False | None | None |
| W1 | Rain tomorrow: umbrella has no tag, reminder only (evening, everything else ready) | pass | READY/READY | -/- | umbrella/umbrella | True/True | 0.64 | 7.99 |
| W2 | Hot and sunny: sunglasses + sunscreen reminders never block READY | pass | READY/READY | -/- | sunglasses|sunscreen/sunglasses|sunscreen | False/False | 0.1 | None |
| W3 | Cold, evening, bag put down, bottle missing: one notification with the reminder | pass | MISSING/MISSING | bottle/bottle | gloves|scarf/gloves|scarf | True/True | 0.88 | 3.82 |
| W4 | Weather unavailable (cache counts as stale: weather_stale_hours = 0): no weather items | pass | READY/READY | -/- | -/- | False/False | 0.01 | None |

## Failures

| part | what | reason |
|---|---|---|
| placement | R1 bag #1 | other tag read: bottle on R1 |
