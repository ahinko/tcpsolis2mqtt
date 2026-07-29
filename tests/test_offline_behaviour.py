"""What each reading means once the datalogger stops answering.

The datalogger is on wifi and only reachable while the inverter is awake, so it is
unreachable every night. It used to publish 0 for everything with
`state_class: measurement`, which is right for a power and wrong for everything
else: AC voltage does not become 0 V, grid frequency does not become 0 Hz and the
inverter is not at 0 degrees.

Not publishing at all is not the middle ground it looks like. Home Assistant keeps
showing the last state and its statistics engine treats a held state as current, so
a stale 600 V sitting there all night pollutes min/max/mean exactly as badly as a
false 0 would. Only unavailable excludes the period, which is what the two
availability topics are for.
"""

import pytest

DATALOGGER_AVAILABILITY = "tcpsolis2mqtt/datalogger_availability"

ZEROED = {
    "active_power",
    "total_dc_output_power",
    "dc1_current",
    "dc2_current",
    "dc3_current",
    "dc4_current",
    "ac_a_phase_current",
    "ac_b_phase_current",
    "ac_c_phase_current",
}

UNAVAILABLE = {
    "inverter_temp",
    "dc1_voltage",
    "dc2_voltage",
    "dc3_voltage",
    "dc4_voltage",
    "ac_a_phase_voltage",
    "ac_b_phase_voltage",
    "ac_c_phase_voltage",
    "grid_frequency",
}


@pytest.fixture
def offline(make_app):
    """An app that has just run out of retries and declared the datalogger offline."""

    def _offline():
        app = make_app()
        app.retries_done = app.config["datalogger"]["poll_retries"]
        app.datalogger_is_offline(offline=True)
        return app

    return _offline


def published(app):
    return {topic.rsplit("/", 1)[-1]: payload for topic, payload in app.published}


def test_power_and_current_are_published_as_zero(offline):
    # active_power especially: a Riemann sum helper integrates it, and a gap would
    # let the trapezoidal rule bridge the night and invent energy out of the two
    # readings either side. This is why the 0 was there in the first place.
    values = published(offline())

    for name in ZEROED:
        assert values[name] == 0, name


def test_voltage_frequency_and_temperature_are_left_to_the_availability_topic(offline):
    values = published(offline())

    for name in UNAVAILABLE:
        assert name not in values, name


def test_the_energy_counters_are_left_alone(offline):
    values = published(offline())

    for name in ["total_power", "generation_today", "generation_yesterday"]:
        assert name not in values, name


def test_the_bit_sensors_fall_back_to_their_default(offline):
    values = published(offline())

    assert values["working_status"] == "Offline"
    assert values["fault_code_01"] == "None"


def test_the_datalogger_is_marked_offline(offline):
    assert (DATALOGGER_AVAILABILITY, "offline") in offline().published


def test_the_datalogger_is_marked_online_again_when_it_answers(offline):
    app = offline()
    app.published.clear()

    app.datalogger_is_offline(offline=False)

    assert (DATALOGGER_AVAILABILITY, "online") in app.published


def test_nothing_is_published_while_the_retries_are_still_running(make_app):
    # A brief dropout must not reach Home Assistant at all, which is what the retry
    # debounce is for: twenty retries at a thirty second poll is about ten minutes.
    app = make_app()

    app.datalogger_is_offline(offline=True)

    assert app.published == []
    assert app.retries_done == 1


def test_every_sensor_depends_on_the_app_being_alive(make_app, sensors_config):
    app = make_app()

    for sensor in sensors_config:
        assert "tcpsolis2mqtt/availability" in app.availability_topics(sensor), sensor[
            "name"
        ]


def test_only_the_unavailable_bucket_depends_on_the_datalogger(
    make_app, sensors_config
):
    app = make_app()
    depends = {
        sensor["name"]
        for sensor in sensors_config
        if DATALOGGER_AVAILABILITY in app.availability_topics(sensor)
    }

    assert depends == UNAVAILABLE


def test_active_power_does_not_depend_on_the_datalogger(make_app, sensors_config):
    # It is published as 0 instead, so making it unavailable would leave the Riemann
    # sum with the gap the 0 exists to prevent.
    app = make_app()
    active_power = next(s for s in sensors_config if s["name"] == "active_power")

    assert app.availability_topics(active_power) == ["tcpsolis2mqtt/availability"]
