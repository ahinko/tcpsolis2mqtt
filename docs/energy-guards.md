# Why the energy readings are guarded

The energy registers are not published as they arrive. Each one is checked first,
and what the check is depends on what the register is. This is the record of why,
because none of it is guessable from the code and all of it was measured rather
than assumed.

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

The statistics this corrupted before the guards existed are being left alone. It
had been happening for over a year by the time it was diagnosed, and rewriting a
year of hourly sums by hand is worse than living with it.

## What a register is

```yaml
# generation_today
modbus:
  resets: daily        # daily | monthly | yearly, absent means never
homeassistant:
  device_class: energy
  state_class: total_increasing
```

Every behaviour is derived from that description rather than configured
separately:

| behaviour | derived from |
| --- | --- |
| plausibility check | `device_class: energy` + `state_class: total_increasing` |
| wall clock reset and stale total guard | `resets:` |
| a counter with no `resets:` must never decrease | absence of `resets:` |
| debounce | `device_class: energy` + no `state_class` |
| dead all zero response | nothing, it is a generic check |

`total_increasing` in Home Assistant means "cumulative counter that may reset to
zero", which is exactly the semantic needed, so reading it is honest rather than
a trick. The wart is that presentation metadata drives data integrity logic, so
`app/sensors.py` validates the invariants at startup and fails loudly:

- `resets:` requires `device_class: energy` and `state_class: total_increasing`
- an energy sensor claims either `total_increasing` or no state class, nothing else

That second one is what makes `is_counter()` and `is_finished_period_total()`
exhaustive. A third kind of energy sensor would fall through both and be published
unguarded.

Only `generation_today`, `energy_this_month` and `generation_this_year` carry
`resets:`. `total_power` is a lifetime counter and deliberately has none.

## The guards

All in `app/app.py`.

- `reset_counters()` publishes `0` when the local date crosses the boundary a
  register resets on. It runs at the top of the poll loop rather than after a
  successful read, because the datalogger is unreachable at midnight. Only the
  date is stored, in a retained topic, because a month and a year boundary are
  both prefixes of it.
- `value_is_plausible()`, for counters, holds three checks:
  - the **plausibility check**, rejecting any increase larger than
    `max_power_kw x elapsed x 1.2 + register resolution`. It is a plausibility
    check, not a rate limit: a rate limit means throttling requests.
  - the **stale total guard**, which remembers the previous period's final value
    at the reset and rejects a reading equal to it (within twice the resolution).
    It disarms on the first reading that is not that value.
  - **never decreasing**, for a counter with no `resets:`. A decrease there is a
    fault, not a reading.
- `value_is_settled()`, for finished period totals, requires a new value to hold
  across three polls before publishing it.
- `response_is_dead()` discards a response where every register is zero.
- `load_state()` restores the above across a restart from retained topics under
  `{prefix}/_state/`, plus the lifetime counter's floor from its own value topic.

### Why the counter guards overlap

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

### Why the finished totals are debounced instead

On 2026-07-28 `generation_yesterday` (register 3015) read 81 for almost the whole
day with four one sample excursions to the correct 103.2, at roughly 04:40, 09:00,
22:30 and 23:50. The baseline was the wrong value and the spikes were right, which
is inverted from `generation_today`. On 2026-07-29 it read 98.8 all morning,
correctly, so it does not happen every day.

A plausibility check is wrong here, since the value legitimately jumps by a whole
day's generation once a day. One sample is not a rollover, though; a real one
persists.

While those three sensors still had `state_class: total_increasing`, each
`81 -> 103.2` excursion was recorded as +22.2 kWh of real growth, about 89 kWh of
phantom energy on 2026-07-28 alone. Removing the state class stopped the damage.
The debounce is the cosmetic half.

## Availability

Two topics, because there are two independent failure modes:

- `{prefix}/availability`, the app is alive. Set as the MQTT Last Will, so the
  broker marks it offline if the container dies. Every sensor references it,
  `active_power` included, since a power reading held forever after a crash is
  worse than any of this. Republished on every connect, because a reconnect
  follows the broker having published the will.
- `{prefix}/datalogger_availability`, the datalogger is reachable. Only the grid
  and environmental sensors reference it.

Sensors that need both list both, with `availability_mode: all`.

What gets published when the datalogger is unreachable comes from `device_class`:

| device_class | when the datalogger is unreachable |
| --- | --- |
| `power`, `current` | publish `0` |
| `voltage`, `frequency`, `temperature` | mark unavailable |
| `energy` | leave alone |

The 0 stays for power because a Riemann sum helper integrates `active_power`, and
a gap would let a trapezoidal integration bridge the night and invent energy.
**This is why the original behaviour was written that way, it was deliberate.**

Not publishing at all is not a middle ground. Home Assistant keeps showing the
last state and the statistics engine treats a held state as current, so a stale
600 V sitting there all night pollutes min/max/mean exactly as badly as a false 0
would. Unavailable is the only option that excludes the period.

DC voltage lands in the unavailable bucket with AC voltage, losing a little truth
since it genuinely is near zero at night. Accepted, there is a separate device
reading the real energy meter.

### Why `poll_retries` is 20, and why a good poll resets the count

`poll_retries` is the offline debounce: that many consecutive failed polls before
`datalogger_availability` flips. At `poll_interval: 30` that is about ten minutes.
It is tuned to this site and is not a default to copy without thinking.

The two failures it has to serve are not the same shape.

**Dusk is continuous.** The inverter stays up until it loses power and then
essentially never comes back the same evening. Failures arrive one after another,
the count climbs cleanly and the datalogger is declared offline once. Observed
2026-08-18: `done 0 of 20` through `done 19 of 20` without a single interruption,
then offline, then the night.

**Dawn is interrupted.** The inverter starts and stops several times before it is
self sustaining. Observed 2026-08-19: six failed polls, one success at 05:35:52,
six more failed polls, then up for good from 05:39:22.

A successful poll resets the count to zero, and that is the mechanism rather than
an oversight. The first reading of the morning says the inverter is on its way up,
and the flapping after it is expected to stop within minutes. Holding
`datalogger_availability` at online across that is the wanted behaviour. The
alternative is Home Assistant marking half a dozen sensors unavailable and back
again several times, every morning of the year, in exchange for describing a state
that is about to end anyway.

It still fires in the morning if the failures stop being interrupted. A datalogger
that comes up briefly and then genuinely dies produces twenty-one unbroken failures
and is declared offline after about ten minutes, so being wrong is bounded rather
than open ended.

The obvious refinements are all worse. See the rejected list below before touching
this.

## Environment this was measured on

- Inverter **S5-GR3P15K, 15 kW**. `max_power_kw` is required in config since
  3.0.0. It is a nameplate rating, not a tuning knob.
- Datalogger S2-WL-ST, on wifi, reachable only while the inverter is awake.
- Production runs `poll_interval: 30` and `poll_retries: 20`. The `config.yaml`
  in this repo is a development config and does not match: it runs
  `poll_retries: 3` with MQTT disabled, so a local run cannot double publish. With
  20 retries at 30 seconds the offline debounce is about ten minutes, which is why
  brief dropouts never reach Home Assistant. Why that number, and why a successful
  poll resets the count, is under Availability above.
- The inverter's own clock runs about **42 minutes slow** but advances correctly
  and ran continuously across the night. It is not a staleness signal.
- At dawn the inverter starts and stops several times before it is
  self sustaining. On 2026-07-29 there were five online to offline transitions
  between 05:30 and 05:43.

## Approaches already rejected

Do not re-propose these without new evidence.

- **Cross checking `generation_today` against `total_power`.** Today's figure
  should equal how far the lifetime counter moved since the day started.
  Rejected: it chains one suspect register onto another, and `total_power` had
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
- **Publishing nothing instead of 0 when offline.** Rejected for `power` and
  `current`: Home Assistant treats a held state as current, so it pollutes
  statistics just as badly. The answer for everything else is `unavailable`, not
  silence.
- **Lowering `poll_retries` so the availability topic tracks the datalogger more
  closely.** Rejected on the numbers. At `poll_retries: 5` the morning of 2026-08-19
  would have been declared offline at 05:35:27, and `poll_interval_if_off` is 600, so
  nothing would have been asked again until 05:45:27. The datalogger was back at
  05:35:52 and healthy for good from 05:39:22. That trades an availability topic
  which was wrong for six and a half minutes, about voltages that were roughly right
  anyway at a time of day when power is near zero, for ten minutes of real data lost
  at the start of the generation day.
- **A progressive backoff after going offline**, starting at `poll_interval` and
  growing towards `poll_interval_if_off`, so a morning flap is retried quickly while
  a winter night still settles at ten minutes. Rejected because there is nothing here
  for it to fix. Offline is declared at dusk, where recovery does not matter until
  morning, and during power outages, which happen once or twice a year, take the
  inverter and therefore the datalogger with them, and are long rather than
  intermittent. The only cost it removes is that sunrise can go unnoticed for up to
  ten minutes after the night's backoff, and generation in the first ten minutes of a
  browning out inverter is nil.
- **Treating a mid-day dropout as an energy risk.** The argument was that holding the
  last `active_power` for ten minutes lets the Riemann sum bridge the gap and invent
  energy. It does not apply here: the datalogger is powered by the inverter, so an
  outage takes both, and usually the broker and Home Assistant with them. Once or
  twice a year, well under a kWh, against annual generation in the thousands.
- **A plausibility check on `generation_yesterday`.** Rejected: the value
  legitimately jumps by a whole day's generation once a day. A debounce fits the
  actual failure, which is one sample wide.

## Verifying a change

```sh
python3 -m pip install -r requirements.txt
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -q
```

Two separate installs on purpose. `amqtt` pins `pyyaml==6.0.2` and
`requirements.txt` asks for `6.0.3`, so a single `pip install -r ... -r ...`
fails to resolve. Installing in sequence lets the dev requirement win, which is
what CI does.

Most tests need no broker; they build an `App` with MQTT disabled and record
publishes. `tests/test_mqtt_state.py` runs a real in-process `amqtt` broker,
because what is being relied on is broker behaviour: retained delivery on
subscribe, empty topics only detectable by timeout, and the will. It uses QoS 1 so
a wait means the broker acknowledged rather than that bytes reached the socket.

`tests/test_plausibility.py` carries the real scenarios and the real numbers.
Keep it that way, they are the regression surface.

Beyond the suite, the strongest check is replaying a real pod log: extract the
`Publishing sensor` lines with their timestamps, feed the values through
`value_is_publishable()` with the real elapsed times, and confirm nothing
legitimate is rejected.

Latest replay, 48 hours to 2026-07-29, 966 polls that reached each energy sensor:

| sensor | accepted | rejected |
| --- | --- | --- |
| generation_today | 966 | 0 |
| total_power | 966 | 0 |
| energy_this_month | 966 | 0 |
| generation_this_year | 966 | 0 |
| generation_yesterday | 962 | 4 |
| generation_last_month | 964 | 2 |
| generation_last_year | 964 | 2 |

Every rejection is the debounce warming up: two polls for the first value of the
run, and two more for `generation_yesterday` when it genuinely rolled over from
103.2 to 98.8 at dawn on 2026-07-29. Nothing legitimate was lost. The wall clock
reset fired once, for `generation_today`, at the one midnight in the window.

That window does not contain the 2026-07-28 anomaly itself, because the container
had restarted since and `kubectl logs -p` had nothing. That trace is encoded in
`tests/test_plausibility.py::test_observed_sunrise_trace` with its real numbers.

## Known residual risks

- If the inverter ever continued counting from yesterday's total instead of
  resetting, the stale value guard would disarm on the first non matching
  reading and publish it. No evidence it does this, ours demonstrably resets to
  0, so it is documented rather than built for.
- A lifetime counter that is rejected for decreasing stays rejected. That is the
  deliberate trade: a `total_power` going backwards is a fault rather than a
  reading, and it is logged as an error. Accepting the drop instead would strand
  the sensor for about three months, since climbing back to 39901 needs that much
  plausibility allowance.
- The debounce is not restored across a restart, so a finished period total takes
  three polls to appear after one. Home Assistant shows the retained value
  meanwhile, which is the same value except in the case the debounce exists for.

## A misdiagnosis worth keeping

The MQTT reconnect loop was recorded for a while as "MQTT callbacks never fire",
and that was wrong in a way worth remembering.

The reasoning was that `Mqtt` defined methods named `on_connect` and
`on_disconnect` which shadow paho's property descriptors, so
`self.on_connect = self.on_connect` set a plain instance attribute, the property
setter never ran, and `_on_connect` stayed `None`. All of that is true, and
`_on_connect` really was `None`.

But paho dispatches off the *public* attribute (`client.py:3906`,
`on_connect = self.on_connect`), not the private one, so the shadowed methods
fired all along. Which means the manual reconnect loop in `on_disconnect` was not
dead code: it ran on paho's own network thread and undid deliberate disconnects,
logging "MQTT Disconnected", then "MQTT Reconnecting in 1 seconds", then "MQTT
Reconnected successfully!".

`loop_start()` already reconnects, so the loop is gone and the handlers are named
`_handle_connect` and `_handle_disconnect`. `tests/test_mqtt_state.py` pins both
the connect callback and that a deliberate disconnect stays disconnected.

The lesson is narrow but real: reading the library's dispatch site would have
settled it in a minute, and reasoning about descriptor shadowing did not.

## History

- `128dd01` reject dead all zero register responses
- `2804180` reject implausible jumps in the daily generation counter
- `721797c` reset the daily counter on the wall clock at midnight
- `e60c062` recognise yesterday's total when the inverter has not reset yet
- `13dbf86` drop the state class from the finished period totals
- `3013074` state when a register resets, derive the rest
- `78dd3b3` stop the retry loop spinning, and count each chunk once
- `bab5eaf` availability topics, and offline values that are true
- `f185426` debounce the finished period totals
- `a3c155a` require `inverter.max_power_kw`, released as 3.0.0

### The bugs in `78dd3b3`

- **Hot retry loop in `query_modbus()`.** A chunk that raised was retried without
  advancing and without sleeping, so the loop spun as fast as the network allowed.
  Retries per poll cycle on the morning of 2026-07-29: 73, 433, 447, 405, 473.
  Roughly two requests per second sustained for minutes, 1828 failed reads over
  thirteen hours, all concentrated in the 05:30-05:45 window when the datalogger
  was already struggling to stay up. Now three attempts two seconds apart, then
  the poll is abandoned.
- **Complete responses were discarded.** `queried_registers_counter += chunk_size`
  ran on every attempt including retries, so the validation compared 80 received
  registers against an inflated 5840. Five complete 80 register responses were
  thrown away on 2026-07-29 for this reason alone. It also meant
  `response_is_dead()` was rarely reached during a flapping period, because the
  length check rejected first.
- **The validation log message had its labels swapped.**
- **`poll_retries` was off by one**, allowing N+1 attempts.
