#!/usr/bin/python3

import os
import yaml
import logging

from config import AppConfig

from time import sleep
from datetime import datetime

from mqtt import Mqtt
from mqtt_discovery import DiscoverMsgSensor

VERSION="0.9.0"
from pymodbus import pymodbus_apply_logging_config
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.transaction import ModbusSocketFramer
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder

class App:
  def __init__(self):
    self.datalogger_offline = False
    self.init_config()
    self.load_register_config()

    if self.config.mqtt.enabled:
      self.mqtt = Mqtt(self.config.mqtt)

    log_level = logging.DEBUG if self.config.debug else logging.INFO
    logging.getLogger().setLevel(log_level)
    pymodbus_apply_logging_config(logging.INFO)

  def init_config(self) -> None:
    config_file = os.environ.get("CONFIG_FILE", "./config.yaml")
    self.config = AppConfig.parse_file(config_file)

  def load_register_config(self) -> None:
    register_file = os.environ.get("REGISTER_FILE", "./modbus.yaml")

    with open(register_file) as file:
      self.register_config = yaml.load(file, yaml.Loader)

  def publish(self, topic, value, retain):
    if not self.config.mqtt.enabled:
      return

    self.mqtt.publish(topic, value, retain)

  def generate_ha_discovery_topics(self):
    for entry in self.register_config:
      if entry['active'] and 'homeassistant' in entry:
        if entry['homeassistant']['device'] == 'sensor':
          logging.info("Generating discovery topic for sensor: "+entry['name'])
          self.publish(f"homeassistant/sensor/{self.config.mqtt.topic_prefix}/{entry['name']}/config",
                            str(DiscoverMsgSensor(self.config.mqtt.topic_prefix,
                                                  entry['description'],
                                                  entry['name'],
                                                  entry['unit'],
                                                  entry['homeassistant']['device_class'],
                                                  entry['homeassistant']['state_class'],
                                                  self.config.inverter.name,
                                                  self.config.inverter.model,
                                                  self.config.inverter.manufacturer,
                                                  VERSION)),
                            retain=True)
        else:
          logging.error("Unknown homeassistant device type: "+entry['homeassistant']['device'])

  def datalogger_is_offline(self, *, offline: bool):
    self.datalogger_offline = offline
    # publish "online/offline" to mqtt

    if self.datalogger_offline:
      # loop over all measurements and set value to 0 and publish to mqtt
      for entry in self.register_config:
        if not entry['active'] or 'function_code' not in entry['modbus'] :
          continue

        if 'homeassistant' in entry and entry['homeassistant']['state_class'] == "measurement":
          value = 0
        else:
          continue

        self.publish(f"{self.config.mqtt.topic_prefix}/{entry['name']}", value, retain=True)

  def main(self):
    self.generate_ha_discovery_topics()

    while True:
      logging.debug("Datalogger scan start at " + datetime.now().isoformat())

      try:
        client = ModbusTcpClient(
            self.config.datalogger.host,
            port=self.config.datalogger.port,
            framer=ModbusSocketFramer,
            timeout=10,
            retry_on_empty=True,
            close_comm_on_error=True,
        )
        client.connect()

      except ModbusException as e:
        # in case we didn't have a exception before
        logging.info(f"Datalogger not reachable, retrying in {self.config.datalogger.poll_interval_if_off} seconds")
        if not self.datalogger_offline:
            self.datalogger_is_offline(offline=True)
      else:
        self.datalogger_is_offline(offline=False)

      # Only move on if datalogger is online
      if not self.datalogger_offline:

        for entry in self.register_config:
          if not entry['active'] or 'function_code' not in entry['modbus'] :
            continue

          try:
            if entry['modbus']['read_type'] == 'register':
              message = client.read_input_registers(slave=self.config.datalogger.slave_id, address=entry['modbus']['register'], count=1)

              value = message.registers[0]
              if 'scale' in entry['modbus']:
                value = float(value) * entry['modbus']['scale']

                if 'decimals' in entry['modbus']:
                  value = round(value, entry['modbus']['decimals'])

            if entry['modbus']['read_type'] == 'long':
              message = client.read_input_registers(slave=self.config.datalogger.slave_id, address=entry['modbus']['register'], count=2)
              decoder = BinaryPayloadDecoder.fromRegisters(message.registers, byteorder=Endian.BIG, wordorder=Endian.BIG)

              value = str(decoder.decode_32bit_int())

            elif entry['modbus']['read_type'] == 'composed_datetime':
              message = client.read_input_registers(slave=self.config.datalogger.slave_id, address=entry['modbus']['register'], count=6)

              value = f"20{message.registers[0]:02d}-{message.registers[1]:02d}-{message.registers[2]:02d}T{message.registers[3]:02d}:{message.registers[4]:02d}:{message.registers[5]:02d}"

          except Exception as e:
            if 'homeassistant' in entry and entry['homeassistant']['state_class'] == "measurement":
              value = 0
            else:
              logging.info("something went wrong!")
              continue
          else:
            self.datalogger_is_offline(offline=False)
            logging.debug(f"{entry['modbus']['register']} {entry['description']} : {value}")

          self.publish(f"{self.config.mqtt.topic_prefix}/{entry['name']}", value, retain=True)

      client.close()

      # wait with next poll configured interval, or if datalogger is not responding ten times the interval
      sleep_duration = self.config.datalogger.poll_interval if not self.datalogger_offline else self.config.datalogger.poll_interval_if_off
      logging.debug(f"Datalogger scanning paused for {sleep_duration} seconds")
      sleep(sleep_duration)

if __name__ == '__main__':
  def start_up():
    handler = logging.StreamHandler()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s", handlers=[handler])
    logging.info("Starting up...")
    App().main()

  start_up()
