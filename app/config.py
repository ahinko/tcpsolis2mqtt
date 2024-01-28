from pydantic import BaseModel, Extra, Field, parse_obj_as, validator
from typing import Dict, List, Optional, Tuple, Union
import yaml

class ConfigBaseModel(BaseModel):
  class Config:
    extra = Extra.forbid

class DataLoggerConfig(ConfigBaseModel):
  host: str = Field(default="", title="Data logger Host")
  port: int = Field(default=1883, title="Data logger Port")
  slave_id: int = Field(default=1, title="Slave ID")
  poll_interval: int = Field(default=60, title="Poll interval")
  poll_interval_if_off: int = Field(default=600, title="Poll interval if off")

class MqttConfig(ConfigBaseModel):
  enabled: bool = Field(title="Enable MQTT Communication.", default=True)
  host: str = Field(default="", title="MQTT Host")
  port: int = Field(default=1883, title="MQTT Port")
  topic_prefix: str = Field(default="tcpsolis2mqtt", title="MQTT Topic Prefix")
  client_id: str = Field(default="tcpsolis2mqtt", title="MQTT Client ID")
  user: Optional[str] = Field(default="", title="MQTT Username")
  password: Optional[str] = Field(default="", title="MQTT Password")
  use_ssl: Optional[bool] = Field(default=False, title="MQTT Use SSL")
  validate_cert: Optional[bool] = Field(default=False, title="MQTT Validate Cert")

  @validator("password", pre=True, always=True)
  def validate_password(cls, v, values):
      if (v is None) != (values["user"] is None):
          raise ValueError("Password must be provided with username.")
      return v

class InverterConfig(ConfigBaseModel):
  name: str = Field(default="", title="Inverter name")
  manufacturer: str = Field(default="", title="Inverter manufacturer")
  model: str = Field(default="", title="Inverter model")

class MqttConfig(ConfigBaseModel):
  datalogger: DataLoggerConfig = Field(title="Data logger Configuration.")
  mqtt: MqttConfig = Field(title="MQTT Configuration.")
  inverter: InverterConfig = Field(title="Inverter Configuration.")

  @classmethod
  def parse_file(cls, config_file):
    with open(config_file) as f:
      raw_config = f.read()

    config = yaml.load(raw_config, yaml.Loader)

    return cls.parse_obj(config)