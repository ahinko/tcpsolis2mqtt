# TODO

Work in progress on the morning energy spike. This file is written so the work
can be picked up cold, without the conversation that produced it.

## The problem

Most mornings Home Assistant recorded a spike of the previous day's entire
generation, over 100 kWh in a single hour. Because the Energy dashboard derives
house consumption from production, both production and consumption were inflated
by the same amount, corrupting the long term statistics.

Cause, from the Home Assistant history on 2026-07-28:

```
00:00-04:49   103.2     yesterday's total, retained, no polls succeed overnight
~04:49         0.0      inverter wakes and clears register 3014
~04:50       103.2      inverter drops out again, the stale value is served
~05:00         0.0      inverter comes back up for good
07:00 on      climbs    a normal day
```

`generation_today` is `state_class: total_increasing`, so Home Assistant reads
the `0 -> 103.2` rise as 103.2 kWh genuinely generated in that hour. The later
drop back to 0 is correctly treated as a counter reset and is harmless. **The
damaging event is the rise off a low value, not the drop.**

Two things had to be true for it to happen: the inverter kept reporting
yesterday's total after midnight, and something lower was published before it.

## How it works now

Four commits are in. The current guards, all in `app/app.py`:

- `reset_daily_counters()` publishes `0` for `generation_today` when the local
  date changes. It runs at the top of the poll loop rather than after a
  successful read, because the datalogger is unreachable at midnight. The day it
  last reset is stored in a retained topic so a restart does not replay it.
- `value_is_plausible()` holds two checks:
  - the **plausibility check**, rejecting any increase larger than
    `max_power_kw x elapsed x 1.2 + register resolution`. A decrease is always
    allowed, that is what a real counter reset looks like.
  - the **stale total guard**, which remembers yesterday's final value at the
    reset and rejects a reading equal to it (within twice the resolution). It
    disarms on the first reading that is not that value.
- `response_is_dead()` discards a response where a register flagged `never_zero`
  reads zero.
- `load_state()` / `value_before_reset()` restore the above across a restart from
  retained topics under `{prefix}/_state/`.

### Why both guards exist

They cover different holes. Measured by disabling each in turn:

| scenario | plausibility only | stale guard only | both |
| --- | --- | --- | --- |
| observed `0 -> 103.2 -> 0` | safe | leaks 103.2 twice | safe |
| stale value first, `40 -> 0 -> 40` | leaks 40.0 | leaks 40.0 | safe |
| mid day flicker `0 -> 5 -> 103.2` | safe | leaks 103.2 | safe |

The plausibility check is the primary guard and the only thing that catches the
real observed trace, because the first reading of that morning was `0.0`, which
disarms the stale guard before `103.2` arrives.

The stale guard covers exactly one hole: the first reading after the overnight
gap. There are no successful polls between roughly 22:47 and 04:49, so by
morning the plausibility allowance has grown to about 87 kWh. That is enough to
catch the observed 103.2 but not a cloudy day's 40 kWh.

## Environment

- Inverter **S5-GR3P15K, 15 kW**. `max_power_kw: 15` under `inverter:` in config,
  defaulting to 15 if absent. This is a nameplate rating, not a tuning knob.
- Datalogger S2-WL-ST, on wifi, reachable only while the inverter is awake.
- Production runs `poll_interval: 30` and `poll_retries: 20`. The `config.yaml`
  in this repo says `poll_retries: 3`, so **the local file does not match what is
  deployed**. With 20 retries at 30 seconds the offline debounce is about ten
  minutes, which is why brief dropouts never reach Home Assistant.
- The inverter's own clock runs about **42 minutes slow** but advances correctly
  and ran continuously across the night. It is not a staleness signal.
- At dawn the inverter starts and stops several times before it is
  self sustaining. On 2026-07-29 there were five online to offline transitions
  between 05:30 and 05:43.
- Logs: `kubectl logs -n home-automation deploy/tcpsolis2mqtt`.
- **The user does not run `main` until we agree it is stable.** Nothing here is
  live yet.

## Decisions

### Terminology

The guard that rejects a reading claiming more energy than the inverter could
have produced is a **plausibility check**. It is not a rate limit, which means
throttling requests and is not what this does. `value_is_plausible()` and the
"at most X was possible" log message are already correct; the yaml flag and a few
comments still say "rate limit" and should not.

### One setting, not three

`never_zero`, `rate_limited` and `resets_daily` collapse into a single field that
states a fact about the register rather than naming a mitigation:

```yaml
# generation_today
resets: daily        # daily | monthly | yearly, absent means never
```

Only `generation_today`, `energy_this_month` and `generation_this_year` need it.
Everything else is derived:

| behaviour | derived from |
| --- | --- |
| plausibility check | `device_class: energy` + `state_class: total_increasing` |
| wall clock reset and stale total guard | `resets:` |
| a lifetime counter must never decrease | absence of `resets:` |
| dead all zero response | nothing, it becomes a generic check |

`total_increasing` in Home Assistant means "cumulative counter that may reset to
zero", which is exactly the semantic needed, so reading it is honest rather than
a trick. The wart is that presentation metadata then drives data integrity logic,
so add a schema validation: `resets:` requires `state_class: total_increasing`,
and the two cannot drift apart without failing at startup.

Dropping `never_zero` is safe because "no `resets:` means it must never decrease"
prevents a bogus 0 reaching `total_power` in the first place. Without that a 0
would land, the plausibility check would then reject the recovery jump back to
39901 as impossible, and since a lifetime counter needs roughly 2200 hours of
allowance to climb that far the sensor would sit stuck at 0 for about three
months.

Deliberate trade: if `total_power` ever legitimately decreased we would now
reject it permanently. A lifetime counter going backwards is a fault rather than
a reading, and it is logged loudly.

The generic dead response check replaces `never_zero`: reject a response where
*every* register is zero. A live inverter cannot produce that, AC voltage alone
reads about 2300.

### Offline behaviour comes from `device_class`

`datalogger_is_offline()` currently publishes `0` for every sensor with
`state_class: measurement`. Split it by `device_class` instead:

| device_class | when the datalogger is unreachable |
| --- | --- |
| `power`, `current` | publish `0` |
| `voltage`, `frequency`, `temperature` | mark unavailable |
| `energy` | leave alone, as now |

The 0 stays for power because a Riemann sum helper integrates `active_power`, and
a gap would let a trapezoidal integration bridge the night and invent energy.
**This is why the current behaviour was written that way, it was deliberate.** It
is still wrong for grid and environmental readings: AC voltage does not become
0 V, grid frequency does not become 0 Hz, the inverter is not at 0 degrees.

DC voltage lands in the unavailable bucket with AC voltage, losing a little truth
since it genuinely is near zero at night. Accepted, there is a separate device
reading the real energy meter.

Not publishing at all is not a middle ground. Home Assistant keeps showing the
last state and the statistics engine treats a held state as current, so a stale
600 V sitting there all night pollutes min/max/mean exactly as badly as a false 0
would. Unavailable is the only option that excludes the period.

Two availability topics, because there are two independent failure modes:

- `{prefix}/availability`, the app is alive, set as the MQTT Last Will so the
  broker marks it offline if the container dies. Every sensor references it,
  including `active_power`, since a power reading held forever after a crash is
  worse than any of this.
- `{prefix}/datalogger_availability`, the datalogger is reachable. Only the grid
  and environmental sensors reference it.

Home Assistant supports a list with `availability_mode: all`, so a sensor can
require both.

## Planned work, in order

1. **Fix the state classes.** `generation_yesterday` (3015),
   `generation_last_month` (3012) and `generation_last_year` (3018) are marked
   `total_increasing` but are step functions, not counters. They should have no
   `state_class` at all. This must land first, because the derivation above
   depends on `total_increasing` identifying exactly the four real counters, and
   right now all seven energy sensors claim it.
2. **The `resets:` refactor.** Replace the three booleans, derive the rest,
   extend the reset and stale guard logic to month and year boundaries, add the
   schema validation.
3. **The retry loop and counter bugs.** See below. Worth doing before the next
   round of log reading, they make the logs unusable.
4. **Availability topics** and the `device_class` driven offline behaviour.
5. **The "yesterday" sensor excursions.** See below.
6. **Clean the existing statistics.** Home Assistant Developer Tools ->
   Statistics, adjust the affected hours. Roughly 103 kWh on 2026-07-28 from
   `generation_today`, plus about 89 kWh the same day from `generation_yesterday`
   (four excursions of +22.2 kWh). Do this last, once nothing new is being
   written.

## Bugs

### Hot retry loop in `query_modbus()`

When a chunk read raises, `current_register` is never advanced and there is no
sleep, so the `while` loop spins as fast as the network allows. Retries per poll
cycle on the morning of 2026-07-29: 73, 433, 447, 405, 473. Roughly two requests
per second sustained for minutes, 1828 failed reads over thirteen hours, all
concentrated in the 05:30-05:45 window when the datalogger was already struggling
to stay up.

Fix: give up on a chunk after a few attempts, sleep briefly between them.

### Complete responses are discarded

`queried_registers_counter += chunk_size` runs on every attempt including
retries, so the validation compares 80 received registers against an inflated
5840. Five complete 80 register responses were thrown away on 2026-07-29 for this
reason alone. It also means `response_is_dead()` is rarely reached during a
flapping period, because the length check rejects first.

Fix: count each chunk once, not once per attempt.

### Validation log message has its labels swapped

Prints `len(registers)` as "Queried" and the counter as "received".

### MQTT callbacks never fire

`Mqtt` in `app/mqtt.py` defines methods named `on_connect` and `on_disconnect`,
which shadow paho's property descriptors of the same name. The property setters
never run, so `_on_connect` and `_on_disconnect` stay `None`. Confirmed against
paho 2.1.0.

Consequences: "MQTT Connected to Broker!" is never logged, and the manual
reconnect loop in `on_disconnect` is dead code. Harmless in practice because
`loop_start()` reconnects on its own, but the code reads as if it does something.
Note `on_message` is *not* shadowed, which is why `read_retained()` works.

Fix: rename the methods and assign them, or drop the dead reconnect loop.

### `poll_retries` is off by one

`if self.retries_done <= poll_retries` allows N+1 attempts before declaring the
datalogger offline.

## The "yesterday" sensor excursions

On 2026-07-28 `generation_yesterday` (register 3015) read 81 for almost the whole
day with four one sample excursions to the correct 103.2, at roughly 04:40,
09:00, 22:30 and 23:50. The baseline was the wrong value and the spikes were
right, which is inverted from `generation_today`. On 2026-07-29 it read 98.8 all
morning, correctly, so it does not happen every day.

With `total_increasing` still set, each 81 -> 103.2 excursion is recorded as
+22.2 kWh of real growth, about 89 kWh of phantom energy on 2026-07-28 alone.
Removing the state class stops the damage; the excursions are cosmetic after
that.

A plausibility check is wrong here since the value legitimately jumps once a day.
A debounce fits better: require a new value to repeat across a few polls before
publishing it, since the excursions are one sample wide and a genuine rollover
persists.

## Approaches already rejected

Do not re-propose these without new evidence.

- **Cross checking `generation_today` against `total_power`.** Today's figure
  should equal how far the lifetime counter moved since the day started.
  Rejected: it chains one suspect register onto another, and `total_power` has
  only been observed as reliable for about a week. If it ever glitched the check
  would silently reject *valid* data.
- **An absolute ceiling of `15 kW x hours since midnight`.** Rejected: at 04:35
  that is 82 kWh, which catches the observed 103.2 but not a cloudy day's 40 kWh.
  A low generation day slips straight under it.
- **Using the inverter clock (register 3072) to detect stale data**, either
  absolutely or by requiring it to advance. Rejected on measurement: through the
  entire anomaly window on 2026-07-28 the clock advanced normally, one minute per
  real minute, monotonic, constant 42 minute offset. The datalogger serves a
  *fresh* response that happens to contain yesterday's number, so there is no
  staleness signal to find.
- **Capping how far the plausibility allowance can grow.** Rejected: it breaks
  legitimate catch up after an outage, and a reading that stays rejected forever
  leaves the sensor stuck.
- **Publishing nothing instead of 0 when offline.** Rejected: Home Assistant
  treats a held state as current, so it pollutes statistics just as badly.

## How to verify a change

```sh
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests -q
```

30 tests. Most need no broker, they build an `App` with MQTT disabled and record
publishes. `tests/test_mqtt_state.py` runs a real in-process `amqtt` broker,
because what is being relied on is broker behaviour: retained delivery on
subscribe, and empty topics only detectable by timeout. It uses QoS 1 so a wait
means the broker acknowledged rather than that bytes reached the socket.

`tests/test_plausibility.py` carries the real scenarios and the real numbers.
Keep it that way, they are the regression surface.

Beyond the suite, the strongest check is replaying a real pod log: extract the
`Publishing sensor generation_today` lines with their timestamps, feed the values
through `value_is_plausible()` with the real elapsed times, and confirm nothing
legitimate is rejected. The 2026-07-28 to 2026-07-29 log gave 652 accepted, 0
rejected.

## Known residual risks

- If the inverter ever continued counting from yesterday's total instead of
  resetting, the stale value guard would disarm on the first non matching
  reading and publish it. No evidence it does this, ours demonstrably resets to
  0, so it is documented rather than built for.
- The test suite takes about 45 seconds, almost entirely `read_retained` timeouts
  against deliberately empty topics.

## Fixed

- `128dd01` reject dead all zero register responses
- `2804180` reject implausible jumps in the daily generation counter
- `721797c` reset the daily counter on the wall clock at midnight
- `e60c062` recognise yesterday's total when the inverter has not reset yet
