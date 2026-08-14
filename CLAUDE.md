# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Polls a Solis S2-WL-ST data logger over Modbus TCP and republishes the registers to MQTT with Home Assistant auto-discovery. One process, one loop, no framework.

## Commands

```sh
# Install. Two separate installs on purpose: amqtt pins pyyaml==6.0.2 and
# requirements.txt asks for 6.0.3, so a combined `pip install -r a -r b` fails to
# resolve. Sequential installs let the dev requirement win, which is what CI does.
python3 -m pip install -r requirements.txt
python3 -m pip install -r requirements-dev.txt

python3 -m pytest tests -q                              # full suite
python3 -m pytest tests/test_plausibility.py -q         # one file
python3 -m pytest tests -q -k observed_sunrise_trace    # one test

ruff format --check --diff app tests    # CI runs these two, in this order
ruff check app tests
```

Run pytest from the repo root — `tests/conftest.py` puts `app/` on `sys.path`, so
modules are imported as `app`, `mqtt`, `sensors`, not `app.app`.

Running the app locally needs a `config.yaml` (gitignored; copy `config.example.yaml`).
`CONFIG_FILE` and `SENSORS_FILE` override the paths; `MQTT_USER` / `MQTT_PASSWORD`
override the MQTT credentials.

## Architecture

`app/app.py` is the entire application: `App.main()` publishes the discovery topics,
computes the register span, restores state from MQTT, then loops forever over
reset → poll → decode → guard → publish → sleep. The other modules are thin.

* `app/config.py`, `app/sensors.py` — marshmallow schemas for `config.yaml` and
  `sensors.yaml`. These are where invariants are enforced at startup rather than
  discovered at runtime. A cross-field rule belongs here, not in `app.py`.
* `app/mqtt.py` — paho client subclass. Owns the Last Will and `read_retained()`.
* `app/mqtt_discovery.py` — builds the Home Assistant discovery JSON.

`sensors.yaml` is the source of truth for every register. Nothing in `app.py`
hardcodes a register number: the polled address span is derived from the lowest and
highest active `modbus.register`, widened by the read type's width (`long` +1,
`alarm` +3, `composed_datetime` +5). Adding a sensor is a YAML edit.

`VERSION` in `app/app.py` is hand-maintained and ships in the discovery payload as
the device's `sw_version`. Bump it with a release.

### The energy guards — read `docs/energy-guards.md` first

The non-obvious half of this codebase. The data logger intermittently serves a value
belonging to a previous day; Home Assistant records it as real generation, so a
morning shows 100+ kWh that never happened. Several mechanisms exist purely to stop
that, and they interlock:

* **Two kinds of energy register, and no third.** A counter (`device_class: energy`
  + `state_class: total_increasing`) is checked against what the inverter could
  physically have generated since the last accepted reading, scaled by
  `inverter.max_power_kw`. A finished period's total (`device_class: energy`, no
  state class) is a step function, so it is debounced over `DEBOUNCE_POLLS` instead.
  `Sensor.an_energy_sensor_is_a_counter_or_a_finished_total` rejects anything else at
  startup.
* **`modbus.resets: daily|monthly|yearly`** states when the *inverter* clears the
  register; its absence means a lifetime counter that may never decrease. Everything
  else — the wall-clock midnight reset, the "still yesterday's total" check, whether a
  decrease is legal — is derived from it. `Sensor.resets_requires_a_counter` keeps it
  from drifting out of step with the Home Assistant metadata.
* **Retained MQTT topics are the only persistent storage.** `<prefix>/_state/...`
  holds the current day and each counter's pre-reset total so a restart doesn't
  replay a midnight reset and double-count the day. `Mqtt.read_retained()` reads them
  by subscribing and timing out.
* **Two availability topics.** `<prefix>/availability` is the broker's Last Will.
  `<prefix>/datalogger_availability` is this app's, and only the sensors in
  `OFFLINE_UNAVAILABLE` depend on it. Offline behaviour is driven by `device_class`
  via the `OFFLINE_ZERO` / `OFFLINE_UNAVAILABLE` sets in `app.py` — power and current
  publish a real `0` (a Riemann sum helper needs the value to keep arriving),
  voltage/frequency/temperature go unavailable, energy counters hold their last value.

Before changing anything under `app/` or the energy entries in `sensors.yaml`, read
`docs/energy-guards.md`. It records what was measured, and which alternatives (the
inverter clock, the `total_power` cross-check) were tried and rejected.

`tests/test_plausibility.py` carries real traces with real numbers from the incidents
these guards exist for. Keep the numbers; they are the regression surface.
`tests/test_mqtt_state.py` runs a real in-process `amqtt` broker because the
behaviour under test is the broker's. Everything else builds an `App` with MQTT
disabled via the `make_app` fixture and records publishes.

## Build and CI

The Python version is declared **once**, in the `Dockerfile` `FROM` line, and repeated
once in `ruff.toml` as `target-version` where Renovate cannot rewrite it. CI reads it
back out of the Dockerfile with a `sed`. `tests/test_python_version.py` enforces all
three facts and will fail the build if a workflow starts declaring its own version or
if `ruff.toml` falls behind — that failure is the intended signal, fix `ruff.toml`.

Two related traps, both documented in the files themselves:

* The Docker tag is plain `-alpine`, not `-alpineX.Y`. Renovate treats the suffix as
  a compatibility constraint it will not cross, which silently froze the image.
* `ruff.toml` pins an explicit rule set (`E4`, `E7`, `E9`, `F`) rather than inheriting
  ruff's default, because CI installs ruff unpinned and a new default turns unrelated
  PRs red. Adopting a new rule is a deliberate commit of its own.

`target-version = "py314"` means the formatter may emit PEP 758 syntax — an
unparenthesized `except TypeError, ValueError:` in `app.py` is valid 3.14, not a typo,
and does not parse on 3.13.

`build-on-release.yaml` pushes to GHCR on both main and `v*.*.*` tags. The `:main`
build is soaked in the cluster before a version is tagged. Deployment itself is
Flux-managed from the separate homelab repo (which pins the image digest), so it is
not deployable from here.

Commit messages are conventional-commit prefixed (`fix(energy):`, `build:`) with a
body explaining *why*, including what was measured and what was rejected. Match that.
