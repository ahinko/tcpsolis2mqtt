#!/usr/bin/python3

import os
import yaml
import logging
import arrow
import requests

from config import AppConfig

from time import sleep
from datetime import datetime

from mqtt import Mqtt
from mqtt_discovery import DiscoverMsgSensor

from pymodbus import pymodbus_apply_logging_config
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.transaction import ModbusSocketFramer
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder

VERSION = "0.9.2"


class App:
    def __init__(self):
        self.datalogger_offline = True
        self.init_config()
        self.load_register_config()

        if self.config.mqtt.enabled:
            self.mqtt = Mqtt(self.config.mqtt)

        log_level = logging.DEBUG if self.config.debug else logging.INFO
        logging.getLogger().setLevel(log_level)
        pymodbus_apply_logging_config(logging.INFO)

        self.timezone_offset = arrow.now("local").format("ZZ")

    def init_config(self) -> None:
        config_file = os.environ.get("CONFIG_FILE", "./config.yaml")
        self.config = AppConfig.parse_file(config_file)

    def load_register_config(self) -> None:
        register_file = os.environ.get("REGISTER_FILE", "./sensors.yaml")

        with open(register_file) as file:
            self.register_config = yaml.load(file, yaml.Loader)

    def publish(self, topic, value, retain):
        if not self.config.mqtt.enabled:
            return

        self.mqtt.publish(topic, value, retain)

    def generate_ha_discovery_topics(self):
        if not self.config.mqtt.enabled:
            return

        for entry in self.register_config:
            if entry["active"] and "homeassistant" in entry:
                if entry["homeassistant"]["device"] == "sensor":
                    logging.debug(
                        "Generating discovery topic for sensor: " + entry["name"]
                    )
                    self.publish(
                        f"homeassistant/sensor/{self.config.mqtt.topic_prefix}/{entry['name']}/config",
                        str(
                            DiscoverMsgSensor(
                                self.config.mqtt.topic_prefix,
                                entry["description"],
                                entry["name"],
                                entry["unit"],
                                entry["homeassistant"]["device_class"],
                                entry["homeassistant"]["state_class"],
                                self.config.inverter.name,
                                self.config.inverter.model,
                                self.config.inverter.manufacturer,
                                VERSION,
                            )
                        ),
                        retain=True,
                    )
                else:
                    logging.error(
                        "Unknown homeassistant device type: "
                        + entry["homeassistant"]["device"]
                    )

    def query_http(self):
        # Check if http is enabled
        if not self.config.datalogger.http.enabled:
            return

        try:
            ir_url = f"http://{self.config.datalogger.host}/inverter.cgi"
            ir = requests.get(
                ir_url,
                auth=(
                    self.config.datalogger.http.user,
                    self.config.datalogger.http.password,
                ),
            )

            mr_url = f"http://{self.config.datalogger.host}/moniter.cgi"
            mr = requests.get(
                mr_url,
                auth=(
                    self.config.datalogger.http.user,
                    self.config.datalogger.http.password,
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

        for entry in self.register_config:
            if not entry["active"] or "http" not in entry:
                continue

            value = None

            if entry["http"]["inverter"] and ir_registers[entry["http"]["register"]]:
                value = ir_registers[entry["http"]["register"]]
            elif entry["http"]["moniter"] and mr_registers[entry["http"]["register"]]:
                value = mr_registers[entry["http"]["register"]]

            if value:
                logging.info(
                    f"{entry['http']['register']} {entry['description']} : {value}"
                )
                self.publish(
                    f"{self.config.mqtt.topic_prefix}/{entry['name']}",
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
            for entry in self.register_config:
                if not entry["active"]:
                    continue

                if (
                    "homeassistant" in entry
                    and entry["homeassistant"]["state_class"] == "measurement"
                ):
                    value = 0
                else:
                    continue

                self.publish(
                    f"{self.config.mqtt.topic_prefix}/{entry['name']}",
                    value,
                    retain=True,
                )

    def main(self):
        # Generate Home assistant MQTT discovery topics
        self.generate_ha_discovery_topics()

        while True:
            logging.debug("Datalogger scan start at " + datetime.now().isoformat())

            try:
                client = ModbusTcpClient(
                    self.config.datalogger.host,
                    port=self.config.datalogger.port,
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
                    f"Datalogger not reachable, retrying in {self.config.datalogger.poll_interval_if_off} seconds"
                )
                if not self.datalogger_offline:
                    self.datalogger_is_offline(offline=True)

            else:
                self.datalogger_is_offline(offline=False)

            # Only move on if datalogger is online
            if not self.datalogger_offline:
                for entry in self.register_config:
                    if (
                        not entry["active"]
                        or "modbus" not in entry
                        or "read_type" not in entry["modbus"]
                    ):
                        continue

                    try:
                        if entry["modbus"]["read_type"] == "register":
                            message = client.read_input_registers(
                                slave=self.config.datalogger.slave_id,
                                address=entry["modbus"]["register"],
                                count=1,
                            )

                            value = message.registers[0]
                            if "scale" in entry["modbus"]:
                                value = float(value) * entry["modbus"]["scale"]

                                if "decimals" in entry["modbus"]:
                                    value = round(value, entry["modbus"]["decimals"])

                        elif entry["modbus"]["read_type"] == "long":
                            message = client.read_input_registers(
                                slave=self.config.datalogger.slave_id,
                                address=entry["modbus"]["register"],
                                count=2,
                            )
                            decoder = BinaryPayloadDecoder.fromRegisters(
                                message.registers,
                                byteorder=Endian.BIG,
                                wordorder=Endian.BIG,
                            )

                            value = str(decoder.decode_32bit_int())

                        elif entry["modbus"]["read_type"] == "composed_datetime":
                            message = client.read_input_registers(
                                slave=self.config.datalogger.slave_id,
                                address=entry["modbus"]["register"],
                                count=6,
                            )

                            value = f"20{message.registers[0]:02d}-{message.registers[1]:02d}-{message.registers[2]:02d}T{message.registers[3]:02d}:{message.registers[4]:02d}:{message.registers[5]:02d}{self.timezone_offset}"

                    except Exception as e:
                        logging.error(f"Error occured {e}")

                        if (
                            "homeassistant" in entry
                            and entry["homeassistant"]["state_class"] == "measurement"
                        ):
                            value = 0
                        else:
                            logging.error("Error while querying data logger: %s", e)
                            continue
                    else:
                        self.datalogger_is_offline(offline=False)
                        logging.info(
                            f"{entry['modbus']['register']} {entry['description']} : {value}"
                        )

                    self.publish(
                        f"{self.config.mqtt.topic_prefix}/{entry['name']}",
                        value,
                        retain=True,
                    )

            client.close()

            # wait with next poll configured interval, or if datalogger is not responding ten times the interval
            sleep_duration = (
                self.config.datalogger.poll_interval
                if not self.datalogger_offline
                else self.config.datalogger.poll_interval_if_off
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
