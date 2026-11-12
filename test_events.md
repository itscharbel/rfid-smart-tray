# Test calendar events (generated from scenarios.json, do not edit by hand)

Create these in the Google Calendar the tray reads, time zone **Europe/Vienna**, as normal timed
events (not all-day). Copy the description exactly, including `Porta:`.

The session runs on **Tuesday 2026-11-17**. Each scenario simulates its own time (`fake_now`), so the
events are spread over **Tue 17 to Sat 21 Nov**: that way each scenario sees exactly its events.
**Those days must have no other events** in that calendar: the session checks it before every
scenario and marks a scenario as a setup error if the tray sees anything else that day.

## Tuesday 17 November 2026

| key | title | start | end | description | used by |
|---|---|---|---|---|---|
| tue_work | Work | 08:00 | 12:00 | (empty) | S01, S03, S08 |
| tue_gym | Gym | 17:00 | 18:30 | `Porta: keys` | S01, S03, S08 |

## Wednesday 18 November 2026

| key | title | start | end | description | used by |
|---|---|---|---|---|---|
| wed_work | Work | 09:00 | 17:00 | (empty) | S02, S06, S07, S10, S11, S12, S13, W1, W2, W3, W4 |

## Thursday 19 November 2026

| key | title | start | end | description | used by |
|---|---|---|---|---|---|
| thu_dinner | Dinner with Marco | 19:00 | 21:00 | `Porta: keys, towel` | S04 |

## Friday 20 November 2026

| key | title | start | end | description | used by |
|---|---|---|---|---|---|
| fri_walk | Walk in the park | 10:00 | 12:00 | `Porta: umbrella, bottle` | S05, S09 |
| fri_meeting | Team meeting | 15:00 | 16:00 | (empty) | S05, S09 |

Days that must stay **empty** (no events at all): 2026-11-21

## Also needed on the tray

- Tags named exactly: `bag`, `laptop`, `bottle`, `towel`, `keys` (`bag` is a tag on your bag).
- **No** tag named `umbrella`, `sunglasses`, `sunscreen`, `scarf`, `gloves` (S09 tests an untagged object; keep the weather items untagged too, so W1-W3 stay predictable).
- Lists (presets) exactly: `gym`: bottle, towel; `work`: laptop, bottle. No other list may match these event titles.

