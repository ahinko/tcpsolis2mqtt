#!/usr/bin/python3

import os
import yaml
import logging
import arrow
import requests

from typing import Any
from config import AppConfig
from sensors import Sensor

from time import sleep
from datetime import datetime

from mqtt import Mqtt
from mqtt_discovery import DiscoverMsgSensor, DiscoverMsgBinary

from pymodbus import pymodbus_apply_logging_config
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.transaction import ModbusSocketFramer
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder

VERSION = "1.0.2"


class App:
    def __init__(self):
        self.datalogger_offline = True
        self.init_config()
        self.load_sensors_config()

        if self.config["mqtt"]["enabled"]:
            self.mqtt = Mqtt(self.config["mqtt"])

        log_level = logging.DEBUG if self.config["debug"] else logging.INFO
        logging.getLogger().setLevel(log_level)
        pymodbus_apply_logging_config(logging.INFO)

        self.timezone_offset = arrow.now("local").format("ZZ")

    def init_config(self) -> None:
        config_file = os.environ.get("CONFIG_FILE", "./config.yaml")
        with open(config_file) as f:
            raw_config = f.read()

        config = yaml.load(raw_config, yaml.Loader)
        self.config = AppConfig().load(config)

    def load_sensors_config(self) -> None:
        sensors_file = os.environ.get("SENSORS_FILE", "./sensors.yaml")

        with open(sensors_file, "r") as file:
            yaml_data = yaml.safe_load(file)

        self.sensors_config = Sensor(many=True).load(yaml_data)

    def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        if not self.config["mqtt"]["enabled"]:
            return

        self.mqtt.publish(topic, payload, retain=retain)

    def generate_ha_discovery_topics(self):
        if not self.config["mqtt"]["enabled"]:
            return

        for sensor in self.sensors_config:
            if sensor["active"] and "homeassistant" in sensor:
                if sensor["homeassistant"]["device"] == "sensor":
                    logging.debug(
                        "Generating discovery topic for sensor: " + sensor["name"]
                    )
                    self.publish(
                        f"homeassistant/sensor/{self.config['mqtt']['topic_prefix']}/{sensor['name']}/config",
                        str(
                            DiscoverMsgSensor(
                                self.config["mqtt"]["topic_prefix"],
                                sensor["description"],
                                sensor["name"],
                                sensor["unit"],
                                sensor["homeassistant"]["device_class"],
                                sensor["homeassistant"]["state_class"],
                                self.config["inverter"]["name"],
                                self.config["inverter"]["model"],
                                self.config["inverter"]["manufacturer"],
                                "http://" + self.config["datalogger"]["host"],
                                VERSION,
                            )
                        ),
                        retain=True,
                    )
                elif sensor["homeassistant"]["device"] == "binary_sensor":
                    logging.debug(
                        "Generating discovery topic for binary sensor: "
                        + sensor["name"]
                    )
                    self.publish(
                        f"homeassistant/binary_sensor/{self.config['mqtt']['topic_prefix']}/{sensor['name']}/config",
                        str(
                            DiscoverMsgBinary(
                                self.config["mqtt"]["topic_prefix"],
                                sensor["description"],
                                sensor["name"],
                                sensor["homeassistant"]["payload_on"],
                                sensor["homeassistant"]["payload_off"],
                                sensor["homeassistant"]["device_class"],
                                sensor["homeassistant"]["state_class"],
                                self.config["inverter"]["name"],
                                self.config["inverter"]["model"],
                                self.config["inverter"]["manufacturer"],
                                "http://" + self.config["datalogger"]["host"],
                                VERSION,
                            )
                        ),
                        retain=True,
                    )
                else:
                    logging.error(
                        "Unknown homeassistant device type: "
                        + sensor["homeassistant"]["device"]
                    )

    def query_http(self):
        # Check if http is enabled
        if not self.config["datalogger"]["http"]["enabled"]:
            return

        try:
            ir_url = f"http://{self.config['datalogger']['host']}/inverter.cgi"
            ir = requests.get(
                ir_url,
                auth=(
                    self.config["datalogger"]["http"]["user"],
                    self.config["datalogger"]["http"]["password"],
                ),
            )

            mr_url = f"http://{self.config['datalogger']['host']}/moniter.cgi"
            mr = requests.get(
                mr_url,
                auth=(
                    self.config["datalogger"]["http"]["user"],
                    self.config["datalogger"]["http"]["password"],
                ),
            )
        except Exception as e:
            logging.error(f"Unable to poll HTTP endpoints {e}")
            return

        if not ir.status_code == 200 or not mr.status_code == 200:
            logging.error("HTTP endpoints did not return 200")
            return

        ir_registers = ir.text.split(";")
        mr_registers = mr.text.split(";")

        for sensor in self.sensors_config:
            if (
                not sensor["active"]
                or "http" not in sensor
                or "endpoint" not in sensor["http"]
                or "register" not in sensor["http"]
            ):
                continue

            value = None

            if (
                sensor["http"]["endpoint"] == "inverter"
                and ir_registers[sensor["http"]["register"]]
            ):
                value = ir_registers[sensor["http"]["register"]]
            elif (
                sensor["http"]["endpoint"] == "moniter"
                and mr_registers[sensor["http"]["register"]]
            ):
                value = mr_registers[sensor["http"]["register"]]

            if value:
                value = value.strip()
                logging.info(
                    f"{sensor['http']['register']} {sensor['description']} : {value}"
                )
                self.publish(
                    f"{self.config['mqtt']['topic_prefix']}/{sensor['name']}",
                    value,
                    retain=True,
                )

    def datalogger_is_offline(self, *, offline: bool):
        # Check if data logger was offline and now came back online
        if self.datalogger_offline and not offline:
            # Came online
            self.query_http()

        self.datalogger_offline = offline

        if self.datalogger_offline:
            # loop over all measurements and set value to 0 and publish to mqtt
            for sensor in self.sensors_config:
                if not sensor["active"]:
                    continue

                if (
                    "homeassistant" in sensor
                    and sensor["homeassistant"]["state_class"] == "measurement"
                ):
                    value = 0
                elif sensor["modbus"]["read_type"] == "bit":
                    value = sensor["modbus"]["bit"]["default_value"]
                else:
                    continue

                logging.info(
                    f"{sensor['modbus']['register']} {sensor['description']} : {value}"
                )

                self.publish(
                    f"{self.config['mqtt']['topic_prefix']}/{sensor['name']}",
                    value,
                    retain=True,
                )

    def map_bit_to_value(self, mapping, default_value, binary_string):
        # Remove '0b' prefix if present
        if binary_string.startswith("0b"):
            binary_string = binary_string[2:]

        active = []
        for i in range(min(len(binary_string), 16)):
            if binary_string[-(i + 1)] == "1":
                for value, status in mapping.items():
                    if value == i:
                        active.append(status)

        if active:
            return ", ".join(active)
        else:
            return default_value

    def main(self):
        # Generate Home assistant MQTT discovery topics
        self.generate_ha_discovery_topics()

        while True:
            logging.debug("Datalogger scan start at " + datetime.now().isoformat())

            try:
                client = ModbusTcpClient(
                    self.config["datalogger"]["host"],
                    port=self.config["datalogger"]["port"],
                    framer=ModbusSocketFramer,
                    timeout=10,
                    retry_on_empty=False,
                    close_comm_on_error=True,
                )

                if not client.connect():
                    raise ModbusException("This is the exception you expect to handle")

            except ModbusException:
                # in case we didn't have a exception before
                logging.info(
                    f"Datalogger not reachable, retrying in {self.config['datalogger']['poll_interval_if_off']} seconds"
                )
                if not self.datalogger_offline:
                    self.datalogger_is_offline(offline=True)

            else:
                self.datalogger_is_offline(offline=False)

            # Only move on if datalogger is online

            for sensor in self.sensors_config:
                if (
                    not sensor["active"]
                    or "modbus" not in sensor
                    or "read_type" not in sensor["modbus"]
                    or self.datalogger_offline
                ):
                    continue

                try:
                    if sensor["modbus"]["read_type"] == "register":
                        message = client.read_input_registers(
                            slave=self.config["datalogger"]["slave_id"],
                            address=sensor["modbus"]["register"],
                            count=1,
                        )

                        value = message.registers[0]
                        if "scale" in sensor["modbus"]:
                            value = float(value) * sensor["modbus"]["scale"]

                            if "decimals" in sensor["modbus"]:
                                value = round(value, sensor["modbus"]["decimals"])

                    elif sensor["modbus"]["read_type"] == "long":
                        message = client.read_input_registers(
                            slave=self.config["datalogger"]["slave_id"],
                            address=sensor["modbus"]["register"],
                            count=2,
                        )
                        decoder = BinaryPayloadDecoder.fromRegisters(
                            message.registers,
                            byteorder=Endian.BIG,
                            wordorder=Endian.BIG,
                        )

                        value = str(decoder.decode_32bit_int())

                    elif sensor["modbus"]["read_type"] == "composed_datetime":
                        message = client.read_input_registers(
                            slave=self.config["datalogger"]["slave_id"],
                            address=sensor["modbus"]["register"],
                            count=6,
                        )

                        value = f"20{message.registers[0]:02d}-{message.registers[1]:02d}-{message.registers[2]:02d}T{message.registers[3]:02d}:{message.registers[4]:02d}:{message.registers[5]:02d}{self.timezone_offset}"

                    elif sensor["modbus"]["read_type"] == "alarm":
                        message = client.read_input_registers(
                            slave=self.config["datalogger"]["slave_id"],
                            address=sensor["modbus"]["register"],
                            count=4,
                        )

                        value = "OFF"
                        if (
                            message.registers[0] != 0
                            or message.registers[1] != 0
                            or message.registers[2] != 0
                            or message.registers[3] != 0
                        ):
                            value = "ON"

                    elif sensor["modbus"]["read_type"] == "bit":
                        message = client.read_input_registers(
                            slave=self.config["datalogger"]["slave_id"],
                            address=sensor["modbus"]["register"],
                            count=1,
                        )

                        if (
                            "bit" in sensor["modbus"]
                            and "map" in sensor["modbus"]["bit"]
                        ):
                            value = self.map_bit_to_value(
                                sensor["modbus"]["bit"]["map"],
                                sensor["modbus"]["bit"]["default_value"],
                                bin(message.registers[0]),
                            )
                        else:
                            logging.error("Could not find needed modbus.bit.map config")
                            continue

                    else:
                        logging.error(
                            f"modbus.readtype of {sensor['modbus']['read_type']} not supported"
                        )
                        continue

                except Exception as e:
                    logging.error(f"Error occured {e}")

                    if (
                        "homeassistant" in sensor
                        and sensor["homeassistant"]["state_class"] == "measurement"
                    ):
                        value = 0
                    elif (
                        "modbus" in sensor
                        and "bit" in sensor["modbus"]
                        and "default_value" in sensor["modbus"]["bit"]
                    ):
                        value = sensor["modbus"]["bit"]["default_value"]
                    else:
                        logging.error("Error while querying data logger: %s", e)
                        continue

                except ModbusException:
                    # in case we didn't have a exception before
                    logging.info(
                        f"Datalogger no longer reachable, retrying in {self.config['datalogger']['poll_interval_if_off']} seconds"
                    )
                    if not self.datalogger_offline:
                        self.datalogger_is_offline(offline=True)

                else:
                    self.datalogger_is_offline(offline=False)
                    logging.info(
                        f"{sensor['modbus']['register']} {sensor['description']} : {value}"
                    )

                self.publish(
                    f"{self.config['mqtt']['topic_prefix']}/{sensor['name']}",
                    value,
                    retain=True,
                )

            client.close()

            # wait with next poll configured interval, or if datalogger is not responding ten times the interval
            sleep_duration = (
                self.config["datalogger"]["poll_interval"]
                if not self.datalogger_offline
                else self.config["datalogger"]["poll_interval_if_off"]
            )
            logging.debug(f"Datalogger scanning paused for {sleep_duration} seconds")
            sleep(sleep_duration)


if __name__ == "__main__":

    def start_up():
        handler = logging.StreamHandler()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(message)s",
            handlers=[handler],
        )
        logging.info("Starting up...")
        App().main()

    start_up()
