"""What generate_ha_discovery_topics publishes for the real sensors.yaml: the topic
Home Assistant listens on, the retain flag, and which device each entity claims.
Running two instances only works if their entities land on different devices."""

import json

import pytest


@pytest.fixture
def discover(make_app):
    def _discover(prefix="tcpsolis2mqtt", identifier="tcpsolis2mqtt", name="Solis"):
        app = make_app()
        app.config["mqtt"] = {"enabled": True, "topic_prefix": prefix}
        app.config["inverter"] |= {
            "identifier": identifier,
            "name": name,
            "model": "S5-GR3P15K",
            "manufacturer": "Ginlong Technologies",
        }
        retained = []
        app.publish = lambda topic, payload, retain=False: retained.append(
            (topic, json.loads(payload), retain)
        )
        app.generate_ha_discovery_topics()
        return retained

    return _discover


def test_every_active_sensor_with_homeassistant_metadata_is_announced(
    discover, sensors_config
):
    expected = {
        s["name"] for s in sensors_config if s["active"] and "homeassistant" in s
    }

    topics = {topic.rsplit("/", 2)[1] for topic, _, _ in discover()}

    assert topics == expected
    assert expected


def test_discovery_is_retained_so_home_assistant_finds_it_after_a_restart(discover):
    assert all(retain for _, _, retain in discover())


def test_the_topic_is_namespaced_by_the_prefix(discover):
    for topic, message, _ in discover(prefix="roof2"):
        root, kind, prefix, name, leaf = topic.split("/")

        assert root == "homeassistant"
        assert kind in ("sensor", "binary_sensor")
        assert (prefix, leaf) == ("roof2", "config")
        assert message["state_topic"] == f"roof2/{name}"
        assert message["unique_id"] == f"roof2/{name}"


def test_every_entity_claims_the_configured_device(discover):
    messages = discover(identifier="solis_roof2", name="Solis roof 2")

    assert messages
    for _, message, _ in messages:
        assert message["device"]["identifiers"] == "solis_roof2"
        assert message["device"]["name"] == "Solis roof 2"


def test_two_instances_do_not_share_a_device_or_an_entity(discover):
    first = discover(prefix="roof1", identifier="solis_roof1")
    second = discover(prefix="roof2", identifier="solis_roof2")

    devices = {m["device"]["identifiers"] for _, m, _ in first + second}
    unique_ids = [m["unique_id"] for _, m, _ in first + second]

    assert devices == {"solis_roof1", "solis_roof2"}
    assert len(unique_ids) == len(set(unique_ids))


def test_discovery_is_skipped_when_mqtt_is_disabled(make_app):
    app = make_app()
    app.publish = lambda *args, **kwargs: pytest.fail("published with MQTT disabled")

    app.generate_ha_discovery_topics()
