"""The discovery message is the only place Home Assistant learns that a sensor has
an availability topic at all, so what goes into it is worth pinning."""

import json

from mqtt_discovery import DiscoverMsgBinary, DiscoverMsgSensor

APP = "tcpsolis2mqtt/availability"
DATALOGGER = "tcpsolis2mqtt/datalogger_availability"


def sensor_msg(topics):
    return json.loads(
        str(
            DiscoverMsgSensor(
                "tcpsolis2mqtt",
                "AC A phase voltage",
                "ac_a_phase_voltage",
                "V",
                "voltage",
                "measurement",
                topics,
                "Solis",
                "S5-GR3P15K",
                "Ginlong Technologies",
                "http://192.0.2.1",
                "2.0.0",
            )
        )
    )


def binary_msg(topics):
    return json.loads(
        str(
            DiscoverMsgBinary(
                "tcpsolis2mqtt",
                "Alarm",
                "alarm",
                "ON",
                "OFF",
                "problem",
                None,
                topics,
                "Solis",
                "S5-GR3P15K",
                "Ginlong Technologies",
                "http://192.0.2.1",
                "2.0.0",
            )
        )
    )


def test_a_sensor_that_needs_both_topics_requires_both():
    message = sensor_msg([APP, DATALOGGER])

    assert message["availability"] == [{"topic": APP}, {"topic": DATALOGGER}]
    assert message["availability_mode"] == "all"


def test_a_sensor_that_only_needs_the_app_says_so():
    assert sensor_msg([APP])["availability"] == [{"topic": APP}]


def test_a_binary_sensor_carries_availability_too():
    assert binary_msg([APP])["availability"] == [{"topic": APP}]


def test_messages_do_not_share_state():
    # DISCOVERY_MSG is a mutable class attribute, so a message that wrote into it
    # rather than into its own copy would leak into the next sensor.
    first = sensor_msg([APP, DATALOGGER])
    second = sensor_msg([APP])

    assert first["availability"] == [{"topic": APP}, {"topic": DATALOGGER}]
    assert second["availability"] == [{"topic": APP}]
