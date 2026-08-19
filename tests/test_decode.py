"""Turning a block of registers into one sensor's value, read type by read type.

The whole chain used to sit inline in the poll loop, which meant no decode could be
exercised without a poll behind it and every new read type was another branch in the
middle of the loop. It is a table of read types and a function each now, so this is
what those functions do with the registers they are handed.
"""

import pytest

from app import READ_TYPES
from sensors import Modbus

TIMEZONE_OFFSET = "+02:00"


@pytest.fixture
def decode(make_app):
    """Decode one sensor out of the registers a poll returned."""

    def _decode(sensor, registers):
        app = make_app()
        app.timezone_offset = TIMEZONE_OFFSET

        return app.decode_sensor(sensor, registers)

    return _decode


@pytest.fixture
def sensor(sensors_config):
    def _sensor(name):
        return next(s for s in sensors_config if s["name"] == name)

    return _sensor


def modbus_sensor(**modbus):
    # A sensor that sensors.yaml does not have, for the cases it does not carry.
    return {
        "name": "made_up",
        "description": "Made up",
        "active": True,
        "modbus": modbus,
    }


def test_a_register_is_scaled_and_rounded(decode, sensor):
    # inverter_temp is scale 0.1, decimals 1.
    assert decode(sensor("inverter_temp"), {3041: 253}) == 25.3


def test_a_register_without_a_scale_is_the_raw_value(decode):
    reading = modbus_sensor(register=3000, read_type="register")

    assert decode(reading, {3000: 7}) == 7


def test_a_long_spans_two_registers(decode, sensor):
    # generation_last_year, 3018 and 3019: the high word carries 65536 of the total.
    assert decode(sensor("generation_last_year"), {3018: 1, 3019: 4464}) == 70000


def test_a_long_is_signed(decode, sensor):
    # INT32, not two unsigned words. A power register that reads all ones is -1, and
    # publishing it as 4294967295 would be a spike no plausibility check can undo.
    assert decode(sensor("generation_last_year"), {3018: 0xFFFF, 3019: 0xFFFF}) == -1


def test_a_datetime_is_composed_from_six_registers(decode, sensor):
    # system_datetime sits at 3072, so the reading runs to 3077 -- the last register
    # of the span, and the one a chunk size landing exactly on the end used to lose.
    registers = {3072: 26, 3073: 8, 3074: 19, 3075: 5, 3076: 42, 3077: 7}

    assert decode(sensor("system_datetime"), registers) == (
        f"2026-08-19T05:42:07{TIMEZONE_OFFSET}"
    )


def test_an_alarm_is_off_when_its_four_registers_are_clear(decode, sensor):
    registers = {3066: 0, 3067: 0, 3068: 0, 3069: 0}

    assert decode(sensor("alarm"), registers) == "OFF"


@pytest.mark.parametrize("raised", [3066, 3067, 3068, 3069])
def test_an_alarm_is_on_when_any_of_them_is_set(decode, sensor, raised):
    registers = {register: 0 for register in range(3066, 3070)}
    registers[raised] = 1

    assert decode(sensor("alarm"), registers) == "ON"


def test_a_bit_reads_as_the_status_it_maps_to(decode, sensor):
    # working_status bit 4 is Standby.
    assert decode(sensor("working_status"), {3071: 0b10000}) == "Standby"


def test_every_set_bit_is_reported(decode, sensor):
    assert decode(sensor("working_status"), {3071: 0b10001}) == "Normal, Standby"


def test_no_bit_set_is_the_default_value(decode, sensor):
    assert decode(sensor("working_status"), {3071: 0}) == "Offline"


def test_a_bit_without_a_map_is_not_published(decode):
    # The schema requires the map, so this is the belt to its braces.
    reading = modbus_sensor(register=3071, read_type="bit")

    assert decode(reading, {3071: 1}) is None


def test_a_reading_whose_register_is_missing_is_not_published(decode, sensor):
    assert decode(sensor("inverter_temp"), {3040: 250}) is None


def test_a_reading_missing_its_second_register_is_not_published(decode, sensor):
    # A short poll is discarded whole, so this is the case where the register span
    # itself stops before the reading does.
    assert decode(sensor("generation_last_year"), {3018: 1}) is None


def test_an_unsupported_read_type_is_not_published(decode):
    reading = modbus_sensor(register=3000, read_type="float")

    assert decode(reading, {3000: 1}) is None


def test_the_schema_and_the_read_type_table_agree():
    # sensors.yaml is gated by the schema, so a read type it accepts and the table
    # has no entry for would be an unpublishable sensor found at runtime, once a
    # poll, forever.
    choices = {
        choice
        for validator in Modbus().fields["read_type"].validators
        for choice in validator.choices
    }

    assert choices == set(READ_TYPES)
