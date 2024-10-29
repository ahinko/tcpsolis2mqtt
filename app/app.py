#!/usr/bin/python3

import yaml
import logging
import arrow
import requests

from typing import Any
from config import AppConfig
from sensors import Sensor

from time import sleep
from datetime import datetime

from environs import Env

from mqtt import Mqtt
from mqtt_discovery import DiscoverMsgSensor, DiscoverMsgBinary

from pymodbus import pymodbus_apply_logging_config
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException, ModbusIOException
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder

VERSION = "1.1.2"


class App:
    def __init__(self):
        self.datalogger_offline = False
        self.datalogger_unreachable = True
        self.init_config()
        self.load_sensors_config()
        self.retries_done = 0

        self.register_span_start = 0
        self.register_span_end = 0

        if self.config["mqtt"]["enabled"]:
            self.mqtt = Mqtt(self.config["mqtt"])

        log_level = logging.DEBUG if self.config["debug"] else logging.INFO
        logging.getLogger().setLevel(log_level)
        pymodbus_apply_logging_config(logging.INFO)

        self.timezone_offset = arrow.now("local").format("ZZ")

    def init_config(self) -> None:
        env = Env()
        env.read_env()

        # Load config from file
        config_file = env("CONFIG_FILE", "./config.yaml")
        with open(config_file) as f:
            raw_config = f.read()

        config = yaml.load(raw_config, yaml.Loader)

        # Load config from env vars
        with env.prefixed("MQTT_"):
            mqtt_user = env("USER", None)
            mqtt_password = env("PASSWORD", None)

        if mqtt_user is not None:
            config["mqtt"]["user"] = mqtt_user

        if mqtt_password is not None:
            config["mqtt"]["password"] = mqtt_password

        # Load config
        self.config = AppConfig().load(config)

    def load_sensors_config(self) -> None:
        env = Env()
        env.read_env()

        sensors_file = env("SENSORS_FILE", "./sensors.yaml")

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
        # Check if new state if offline
        if offline:
            # Set in unreachable state before offline
            self.datalogger_unreachable = True

            # Check if retries are done
            if self.retries_done <= self.config["datalogger"]["poll_retries"]:
                logging.info(
                    f"Datalogger not reachable, done {self.retries_done} of {self.config['datalogger']['poll_retries']} retries"
                )
                self.retries_done += 1
                return

        # Check if data logger was offline and now came back online
        if (self.datalogger_offline or self.datalogger_unreachable) and not offline:
            # Came online
            self.query_http()
            # Reset retry counter
            self.retries_done = 0
            # Reset unreachable flag
            self.datalogger_unreachable = False

        self.datalogger_offline = offline

        if self.datalogger_offline:
            logging.info("Datalogger offline")
            # loop over all measurements and set value to 0 and publish to mqtt
            for sensor in self.sensors_config:
                if not sensor["active"]:
                    continue

                if (
                    "homeassistant" in sensor
                    and sensor["homeassistant"]["state_class"] == "measurement"
                ):
                    value = 0
                elif "modbus" in sensor and sensor["modbus"]["read_type"] == "bit":
                    value = sensor["modbus"]["bit"]["default_value"]
                else:
                    continue

                if "modbus" in sensor:
                    logging.info(
                        f"{sensor['modbus']['register']} {sensor['description']} : {value}"
                    )
                else:
                    logging.info(
                        f"{sensor['http']['register']} {sensor['description']} : {value}"
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

    def get_register_interval(self):
        # Check if we already have a first and last register
        if self.register_span_start > 0 and self.register_span_end > 0:
            return

        for sensor in self.sensors_config:
            # Check if sensor is active and has a modbus read type, also check if the datalogger is online
            if not sensor["active"] or "modbus" not in sensor:
                continue

            if (
                self.register_span_start == 0
                or sensor["modbus"]["register"] < self.register_span_start
            ):
                self.register_span_start = sensor["modbus"]["register"]

            if sensor["modbus"]["register"] > self.register_span_end:
                additional = 0

                if sensor["modbus"]["read_type"] == "long":
                    additional = 1
                elif sensor["modbus"]["read_type"] == "composed_datetime":
                    additional = 5
                elif sensor["modbus"]["read_type"] == "alarm":
                    additional = 3

                self.register_span_end = sensor["modbus"]["register"] + additional

        logging.info(
            f"First register: {self.register_span_start}, Last register: {self.register_span_end}"
        )

    def query_modbus(self):
        logging.info("Querying modbus")

        try:
            client = ModbusTcpClient(
                self.config["datalogger"]["host"],
                port=self.config["datalogger"]["port"],
                reconnect_delay=10,
                timeout=10,
            )

            if not client.connect():
                raise ModbusException("Client not connected to datalogger")

        except Exception as e:
            logging.error(f"Unable to connect to datalogger: {e}")
            if not self.datalogger_offline:
                self.datalogger_is_offline(offline=True)
            return

        else:
            self.datalogger_is_offline(offline=False)

        registers = {}
        current_register = self.register_span_start
        chunk_size = self.config["datalogger"]["register_chunks"]
        queried_registers_counter = 0

        # Loop while we still have registers to query
        while current_register < self.register_span_end:
            try:
                logging.info(
                    f"Querying register {current_register} to {current_register + chunk_size}"
                )
                queried_registers_counter += chunk_size

                message = client.read_input_registers(
                    slave=self.config["datalogger"]["slave_id"],
                    address=current_register,
                    count=chunk_size,
                )

                if message.isError():
                    raise Exception(
                        "Could not read register, might have lost connection"
                    )

                logging.info(f"Result: {message.registers}")

                registry_number = current_register
                for registry in message.registers:
                    registers[registry_number] = registry
                    registry_number += 1

                current_register += chunk_size

            except (Exception, ModBusIoException) as e:
                logging.error(f"Error occured while querying modbus: {e}")

                if str(e) == "Could not read register, might have lost connection":
                    if not self.datalogger_offline:
                        self.datalogger_is_offline(offline=True)

            else:
                self.datalogger_is_offline(offline=False)

        client.close()

        if client.connected:
            logging.error("Client still connected to datalogger")

        # Sometimes we get a response with almost all values being 0, usually also multiple registers
        # are missing. In that case we just return an empty dictionary. This validation is not perfect
        # but it should be good enough for now.
        if len(registers) != queried_registers_counter:
            logging.info(
                f"Validation of number of queried registers failed. Queried: {len(registers)}, received: {queried_registers_counter}"
            )
            return {}

        return registers

    def pick_from_registers(self, registers, start, count):
        return [registers[i] for i in range(start, start + count)]

    def main(self):
        # Generate Home assistant MQTT discovery topics
        self.generate_ha_discovery_topics()

        # Get register interval, find lowest and higest register numbers
        self.get_register_interval()

        while True:
            logging.debug("Datalogger scan start at " + datetime.now().isoformat())

            ## Query modbus
            registers = self.query_modbus()

            for sensor in self.sensors_config:
                # Check if sensor is active and has a modbus read type, also check if the datalogger is online
                if (
                    not sensor["active"]
                    or "modbus" not in sensor
                    or "read_type" not in sensor["modbus"]
                    or not registers
                ):
                    continue

                try:
                    # Verify that we have the register needed
                    if sensor["modbus"]["register"] not in registers:
                        raise Exception(
                            f"Register {sensor["modbus"]["register"]} not found"
                        )

                    if sensor["modbus"]["read_type"] == "register":
                        # Get value
                        values = self.pick_from_registers(
                            registers,
                            sensor["modbus"]["register"],
                            1,
                        )

                        value = values[0]

                        # Transform value
                        if "scale" in sensor["modbus"]:
                            value = float(value) * sensor["modbus"]["scale"]

                            if "decimals" in sensor["modbus"]:
                                value = round(value, sensor["modbus"]["decimals"])

                    elif sensor["modbus"]["read_type"] == "long":
                        values = self.pick_from_registers(
                            registers,
                            sensor["modbus"]["register"],
                            2,
                        )

                        decoder = BinaryPayloadDecoder.fromRegisters(
                            values,
                            byteorder=Endian.BIG,
                            wordorder=Endian.BIG,
                        )

                        value = str(decoder.decode_32bit_int())

                    elif sensor["modbus"]["read_type"] == "composed_datetime":
                        values = self.pick_from_registers(
                            registers,
                            sensor["modbus"]["register"],
                            6,
                        )

                        value = f"20{values[0]:02d}-{values[1]:02d}-{values[2]:02d}T{values[3]:02d}:{values[4]:02d}:{values[5]:02d}{self.timezone_offset}"

                    elif sensor["modbus"]["read_type"] == "alarm":
                        values = self.pick_from_registers(
                            registers,
                            sensor["modbus"]["register"],
                            4,
                        )

                        value = "OFF"
                        if (
                            values[0] != 0
                            or values[1] != 0
                            or values[2] != 0
                            or values[3] != 0
                        ):
                            value = "ON"

                    elif sensor["modbus"]["read_type"] == "bit":
                        values = self.pick_from_registers(
                            registers,
                            sensor["modbus"]["register"],
                            1,
                        )

                        value = values[0]

                        if (
                            "bit" in sensor["modbus"]
                            and "map" in sensor["modbus"]["bit"]
                        ):
                            value = self.map_bit_to_value(
                                sensor["modbus"]["bit"]["map"],
                                sensor["modbus"]["bit"]["default_value"],
                                bin(value),
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

                else:
                    logging.info("Publishing sensor %s: %s", sensor["name"], value)

                    self.publish(
                        f"{self.config['mqtt']['topic_prefix']}/{sensor['name']}",
                        value,
                        retain=True,
                    )

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
