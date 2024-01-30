import json
from copy import deepcopy

# Generate MQTT discovery message for home-assistant
# for more info: https://www.home-assistant.io/docs/mqtt/discovery/


class DiscoverMsgSensor:
    DISCOVERY_MSG = {
        "name": "",
        "state_topic": "",
        "unique_id": "",
        "device_class": "",
        "state_class": "",
        "unit_of_measurement": "",
        "device": {
            "name": "",
            "model": "",
            "manufacturer": "",
            "identifiers": "tcpsolis2mqtt",
            "sw_version": "tcpsolis2mqtt",
        },
    }

    def __init__(
        self,
        topic_prefix,
        description,
        name,
        unit,
        device_class,
        state_class,
        device_name,
        device_model,
        device_manufacturer,
        version,
    ):
        self.discover_msg = deepcopy(DiscoverMsgSensor.DISCOVERY_MSG)
        self.discover_msg["name"] = description
        self.discover_msg["state_topic"] = topic_prefix + "/" + name
        self.discover_msg["unique_id"] = topic_prefix + "/" + name
        self.discover_msg["device_class"] = device_class
        self.discover_msg["state_class"] = state_class
        self.discover_msg["unit_of_measurement"] = unit
        self.discover_msg["device"]["name"] = device_name
        self.discover_msg["device"]["model"] = device_model
        self.discover_msg["device"]["manufacturer"] = device_manufacturer
        self.discover_msg["device"]["sw_version"] += str(version)

    def __str__(self):
        return json.dumps(self.discover_msg)
