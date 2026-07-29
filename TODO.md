# TODO

Findings from investigating the morning energy spike, and the decisions taken
about how to fix what is left.

## Decisions

### Terminology

The guard that rejects a reading claiming more energy than the inverter could
have produced is a **plausibility check**. It is not a rate limit, which means
throttling requests and is not what this does. `value_is_plausible()` and the
"at most X was possible" log message are already correct; the yaml flag and a few
comments are not.

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
| dead all zero response | nothing, it is a generic check |

`total_increasing` in Home Assistant means "cumulative counter that may reset to
zero", which is exactly the semantic needed, so reading it is honest rather than
a trick. The wart is that presentation metadata now drives data integrity logic,
so add a schema validation: `resets:` requires `state_class: total_increasing`,
and the two cannot drift apart without failing at startup.

Dropping `never_zero` is safe because "no `resets:` means it must never decrease"
prevents a bogus 0 reaching `total_power` in the first place. Without that, a 0
would land, the plausibility check would then reject the recovery jump back to
39901 as impossible, and since a lifetime counter needs roughly 2200 hours of
allowance to climb that far the sensor would sit stuck at 0 for about three
months.

Deliberate trade: if `total_power` ever legitimately decreased we would now
reject it permanently. A lifetime counter going backwards is a fault rather than
a reading, and it is logged loudly.

### Offline behaviour comes from `device_class`

| device_class | when the datalogger is unreachable |
| --- | --- |
| `power`, `current` | publish `0` |
| `voltage`, `frequency`, `temperature` | mark unavailable |
| `energy` | leave alone, as now |

The 0 stays for power because a Riemann sum helper integrates `active_power`, and
a gap would let a trapezoidal integration bridge the night and invent energy. It
is wrong for grid and environmental readings: AC voltage does not become 0 V,
grid frequency does not become 0 Hz, the inverter is not at 0 degrees.

DC voltage lands in the unavailable bucket with AC voltage, losing a little truth
since it genuinely is near zero at night. Accepted, there is a separate device
reading the real energy meter.

Not publishing at all is not a middle ground. Home Assistant keeps showing the
last state and the statistics engine treats a held state as current, so a stale
600 V sitting there all night pollutes min/max/mean exactly as badly as a false
0 would. Unavailable is the only option that excludes the period.

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
   `state_class` at all. This has to land first, because the derivation above
   depends on `total_increasing` identifying exactly the four real counters.
2. **The `resets:` refactor.** Replace the three booleans, derive the rest,
   extend the reset and stale guard logic to month and year boundaries.
3. **The retry loop and counter bugs.** See below.
4. **Availability topics** and the `device_class` driven offline behaviour.
5. **The "yesterday" sensor excursions.** See below.

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
reason alone. It also means the dead response check is rarely reached during a
flapping period, because the length check rejects first.

Fix: count each chunk once, not once per attempt.

### Validation log message has its labels swapped

Prints `len(registers)` as "Queried" and the counter as "received".

### MQTT callbacks never fire

`Mqtt` defines methods named `on_connect` and `on_disconnect`, which shadow
paho's property descriptors of the same name. The property setters never run, so
`_on_connect` and `_on_disconnect` stay `None`. Confirmed against paho 2.1.0.

Consequences: "MQTT Connected to Broker!" is never logged, and the manual
reconnect loop in `on_disconnect` is dead code. Harmless in practice because
`loop_start()` reconnects on its own, but the code reads as if it does something.

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

With `total_increasing` still set, each 81 -> 103.2 excursion was recorded as
+22.2 kWh of real growth, about 89 kWh of phantom energy on 2026-07-28 alone.
Removing the state class stops the damage; the excursions themselves are cosmetic
after that.

A plausibility check is wrong here since the value legitimately jumps once a day.
A debounce fits better: require a new value to repeat across a few polls before
publishing it, since the excursions are one sample wide and a genuine rollover
persists.

## Known residual risks

- If the inverter ever continued counting from yesterday's total instead of
  resetting, the stale value guard would disarm on the first non matching
  reading and publish it. No evidence it does this, ours demonstrably resets
  to 0, so it is documented rather than built for.
- The test suite takes about 45 seconds, almost entirely `read_retained`
  timeouts against deliberately empty topics.

## Fixed

- `128dd01` reject dead all zero register responses
- `2804180` reject implausible jumps in the daily generation counter
- `721797c` reset the daily counter on the wall clock at midnight
- `e60c062` recognise yesterday's total when the inverter has not reset yet
