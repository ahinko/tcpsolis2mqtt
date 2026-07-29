"""The energy sensors carry the metadata that other behaviour is derived from, so
what each of them claims to be is a regression surface of its own.

An energy sensor is one of exactly two things. A counter, `total_increasing`, that
climbs through the period and is checked for plausibility. Or a finished period's
total, no state class, which is a step function and only ever moves at a rollover.
Marking a step function `total_increasing` makes Home Assistant record every step
as energy generated in that hour, which is where roughly 89 kWh of phantom
generation came from on 2026-07-28.
"""

import pytest
from marshmallow import ValidationError
from sensors import Sensor

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


def test_every_resetting_counter_says_which_period(sensors_config):
    resets = {
        sensor["name"]: sensor["modbus"].get("resets")
        for sensor in sensors_config
        if "modbus" in sensor and sensor["name"] in COUNTERS
    }

    assert resets == {
        "total_power": None,
        "generation_today": "daily",
        "energy_this_month": "monthly",
        "generation_this_year": "yearly",
    }


def definition(**modbus):
    return {
        "name": "made_up",
        "description": "Made up",
        "modbus": {"register": 3014, "read_type": "register", "function_code": 4}
        | modbus,
        "homeassistant": {"device": "sensor"},
    }


def test_resets_is_rejected_without_a_state_class():
    # The behaviour is derived from the Home Assistant metadata, so the two
    # descriptions of the same register are not allowed to drift apart.
    with pytest.raises(ValidationError, match="modbus.resets"):
        Sensor().load(definition(resets="daily"))


def test_resets_is_accepted_on_a_counter():
    sensor = definition(resets="daily")
    sensor["homeassistant"] |= {
        "device_class": "energy",
        "state_class": "total_increasing",
    }

    assert Sensor().load(sensor)["modbus"]["resets"] == "daily"


def test_an_unknown_period_is_rejected():
    with pytest.raises(ValidationError):
        Sensor().load(definition(resets="hourly"))


def energy_definition(state_class):
    sensor = definition()
    sensor["homeassistant"] |= {"device_class": "energy", "state_class": state_class}
    return sensor


@pytest.mark.parametrize("state_class", ["measurement", "total"])
def test_an_energy_sensor_may_not_claim_any_other_state_class(state_class):
    # is_counter and is_finished_period_total have to cover every energy register
    # between them, and a third kind would fall through both.
    with pytest.raises(ValidationError, match="energy sensor"):
        Sensor().load(energy_definition(state_class))


@pytest.mark.parametrize("state_class", ["total_increasing", None])
def test_an_energy_sensor_may_be_a_counter_or_a_finished_total(state_class):
    assert Sensor().load(energy_definition(state_class))
