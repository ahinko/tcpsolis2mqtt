# TODO

Things found while investigating the morning energy spike, not yet fixed.

## Bugs

### Hot retry loop in `query_modbus()`

When a chunk read raises, `current_register` is never advanced and there is no
sleep, so the `while` loop spins as fast as the network allows. Retries per poll
cycle on the morning of 2026-07-29: 73, 433, 447, 405, 473. That is roughly two
requests per second sustained for minutes, 1828 failed reads over thirteen hours,
all concentrated in the 05:30-05:45 window when the datalogger was already
struggling to stay up.

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

## The "yesterday" sensor

Its own thread, not yet started.

On 2026-07-28 `generation_yesterday` (register 3015) read 81 for almost the whole
day with four one sample excursions to the correct 103.2, at roughly 04:40,
09:00, 22:30 and 23:50. The baseline was the wrong value and the spikes were
right, which is inverted from `generation_today`. On 2026-07-29 it read 98.8 all
morning, correctly, so it does not happen every day.

Two separate problems:

1. **Wrong state class.** `total_increasing` on a step function. Each 81 -> 103.2
   excursion is recorded by Home Assistant as +22.2 kWh of real growth, about
   89 kWh of phantom energy on 2026-07-28 alone. `generation_last_month` (3012)
   and `generation_last_year` (3018) have the same problem. These should have no
   `state_class` at all.
2. **The excursions themselves.** A rate limit is wrong here since the value
   legitimately jumps once a day. A debounce fits better: require a new value to
   repeat across a few polls before publishing it, since the excursions are one
   sample wide and a genuine rollover persists.

## Design questions

### Publishing 0 for every measurement sensor when offline

`datalogger_is_offline()` publishes 0 for every sensor with
`state_class: measurement`. Defensible for power and current, where the inverter
really is producing nothing. Wrong for grid derived and environmental readings:
AC phase voltage does not become 0 V, grid frequency does not become 0 Hz, and
the inverter is not at 0 degrees. Those are properties of the grid and the
weather, not of our modbus link.

Five offline transitions on the morning of 2026-07-29 means five fake 0 W dips in
the power graphs, and five fake 0 V and 0 Hz readings polluting min/max
statistics.

Options: a per sensor `offline_value` in `sensors.yaml`, or an availability topic
so Home Assistant marks the sensors unavailable instead. Note that an explicit 0
is safer than a gap for any power sensor feeding a Riemann sum integration, so
this is not simply "availability is better".

### Rate limiting is only applied to `generation_today`

`energy_this_month` (3010), `generation_this_year` (3016) and `total_power`
(3008) are cumulative counters with the same exposure, and the flag already
exists.

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
