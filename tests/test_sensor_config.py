"""The energy sensors carry the metadata that other behaviour is derived from, so
what each of them claims to be is a regression surface of its own.

An energy sensor is one of exactly two things. A counter, `total_increasing`, that
climbs through the period and is checked for plausibility. Or a finished period's
total, no state class, which is a step function and only ever moves at a rollover.
Marking a step function `total_increasing` makes Home Assistant record every step
as energy generated in that hour, which is where roughly 89 kWh of phantom
generation came from on 2026-07-28.
"""

COUNTERS = {
    "total_power",
    "generation_today",
    "energy_this_month",
    "generation_this_year",
}

FINISHED_PERIODS = {
    "generation_yesterday",
    "generation_last_month",
    "generation_last_year",
}


def energy_sensors(sensors_config):
    return [
        sensor
        for sensor in sensors_config
        if sensor.get("homeassistant", {}).get("device_class") == "energy"
    ]


def test_the_counters_are_the_only_total_increasing_sensors(sensors_config):
    increasing = {
        sensor["name"]
        for sensor in sensors_config
        if sensor.get("homeassistant", {}).get("state_class") == "total_increasing"
    }

    assert increasing == COUNTERS


def test_a_finished_period_total_has_no_state_class(sensors_config):
    for sensor in energy_sensors(sensors_config):
        if sensor["name"] in FINISHED_PERIODS:
            assert not sensor["homeassistant"]["state_class"], sensor["name"]


def test_every_energy_sensor_is_one_or_the_other(sensors_config):
    assert {sensor["name"] for sensor in energy_sensors(sensors_config)} == (
        COUNTERS | FINISHED_PERIODS
    )
