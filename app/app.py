#!/usr/bin/python3

import yaml
import logging
import arrow
import requests

from typing import Any, Callable, NamedTuple
from config import AppConfig
from sensors import Sensor

from time import monotonic, sleep
from datetime import datetime

from environs import Env

from mqtt import Mqtt, ONLINE, OFFLINE
from mqtt_discovery import DiscoverMsgSensor, DiscoverMsgBinary

from pymodbus import pymodbus_apply_logging_config
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

VERSION = "3.0.0"

# A local date is YYYY-MM-DD, so the period a date belongs to is a prefix of it.
PERIOD_LENGTH = {"daily": 10, "monthly": 7, "yearly": 4}

# How hard to try for one chunk of registers before giving up on the whole poll.
#
# These two describe the whole retry budget only because the client is built with
# retries=0. pymodbus runs its own loop, `while count_retries <= retries`, so its
# stock default of 3 is four attempts per request and multiplies with this one: a
# chunk that never answered cost 3 x 4 x MODBUS_TIMEOUT, a little over two minutes,
# against a poll_interval most people set to 30 or 60.
# The numbers here have to be the only ones that count, or they are not a budget.
CHUNK_ATTEMPTS = 3
CHUNK_RETRY_DELAY = 2

# Seconds to wait for one request to the datalogger. Deliberately left where it has
# always been: how patient we are with a single answer is the one number here that can
# turn a poll that used to succeed into one that fails, and it wants measuring against
# the hardware rather than guessing.
MODBUS_TIMEOUT = 10

# How many polls a finished period's total has to hold a new value before it is
# believed. A rollover happens once a day at most and then persists, so a value that
# appears for one poll and vanishes is the datalogger, not the inverter.
DEBOUNCE_POLLS = 3

# What a reading means once the datalogger stops answering, by what the reading is.
#
# A power or a current really is zero, and active_power has to keep arriving: a
# Riemann sum helper integrates it, and a gap would let the trapezoidal rule bridge
# the night and invent energy out of the two readings either side.
#
# The rest are not zero. Grid voltage does not become 0 V, grid frequency does not
# become 0 Hz, and the inverter is not at 0 degrees. Nor can they simply stop being
# published, because Home Assistant keeps showing the last state and its statistics
# engine treats a held state as current, so a stale 600 V sitting there all night
# pollutes min/max/mean exactly as badly as a false 0 would. Marking them
# unavailable is the only thing that excludes the period.
#
# DC voltage lands in the unavailable bucket with AC voltage, which loses a little
# truth since it genuinely is near zero at night. Accepted.
#
# Anything else, which is to say the energy counters, is left showing its last value.
OFFLINE_ZERO = {"power", "current"}
OFFLINE_UNAVAILABLE = {"voltage", "frequency", "temperature"}

# What the datalogger pads a CGI field with. Captured 2026-08-15: both inverter.cgi
# and moniter.cgi answer with a fixed 1313 byte buffer holding 33 NUL bytes, then the
# semicolon separated fields, then NUL again to the end of the buffer. So the first
# field of a response carries 33 leading NULs and the last one is nothing but padding.
#
# The inverter serial arrived that way, 49 characters of which 33 were NUL. It was the
# only topic affected because inverter_serial is the only sensor in sensors.yaml
# mapped to field 0 of either response, while its sibling datalogger_serial is
# moniter.cgi field 2 and came through as a clean 16 characters. An http sensor has no
# length or field count to get wrong, so there is nothing in the register map to
# correct here. Removing the padding is the decode.
#
# It cannot be left to a consumer. Home Assistant stores the payload verbatim as the
# sensor state and its recorder then INSERTs it into Postgres, which refuses a NUL in
# a text column. That fails the flush and poisons the session, so every other row
# queued in the same commit is discarded along with it.
HTTP_PADDING = "\x00 \t\r\n\v\f"

# Seconds to wait for the datalogger's CGIs: to answer the connection, then for each
# block of the body. requests has no default, so without this a datalogger that
# completes the handshake and then says nothing blocks the poll loop forever.
#
# Nothing notices that. Mqtt.loop_start runs paho's network thread, so the broker
# keeps seeing a live session and the Last Will never fires. Home Assistant sees an
# app that is up and has simply stopped publishing, and every sensor holds the value
# it had at the moment we hung -- which is the stale reading docs/energy-guards.md
# exists to prevent.
#
# query_http only runs on the transition back to online, which is the moment the
# datalogger is least likely to be fully awake and most likely to accept a connection
# it cannot answer.
HTTP_TIMEOUT = (5, 10)


class ReadType(NamedTuple):
    """One kind of modbus reading: how wide it is, and how to turn it into a value."""

    # How many registers the reading spans. The span polled each cycle is derived from
    # this, so a read type that claims fewer registers than it uses truncates the poll.
    width: int
    # Takes the App, the sensor and the registers the reading spans; returns the value
    # to publish, or None if this reading cannot be made from them.
    decode: Callable[["App", dict[str, Any], list[int]], Any]


class App:
    def __init__(self):
        self.datalogger_offline = False
        self.datalogger_unreachable = True
        # What was last put on the availability topic, so the same answer is not sent
        # again. None until the first poll decides, which is what makes that first
        # publish happen whichever way it goes.
        self.availability_published = None
        self.init_config()
        self.load_sensors_config()
        self.retries_done = 0

        self.register_span_start = 0
        self.register_span_end = 0

        self.last_accepted_value = {}
        self.current_day = None
        self.previous_period_total = {}
        self.awaiting_new_period = {}
        self.settled_value = {}
        self.pending_value = {}

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

    def local_date(self):
        return arrow.now("local").format("YYYY-MM-DD")

    def device_class(self, sensor: dict[str, Any]) -> str | None:
        return sensor.get("homeassistant", {}).get("device_class")

    def availability_topics(self, sensor):
        # Two topics, because there are two independent failure modes. The app being
        # alive is the broker's business, via the will. The datalogger being reachable
        # is this app's, and only the readings that cannot be faked as 0 care about it.
        prefix = self.config["mqtt"]["topic_prefix"]
        topics = [f"{prefix}/availability"]

        if self.device_class(sensor) in OFFLINE_UNAVAILABLE:
            topics.append(f"{prefix}/datalogger_availability")

        return topics

    def is_counter(self, sensor):
        # A cumulative energy counter, which is the only kind of reading whose value
        # can be sanity checked: it can only climb, and only as fast as the inverter
        # is able to generate. total_increasing in Home Assistant means exactly
        # "cumulative counter that may reset to zero", so reading it here is honest
        # rather than a trick, and the sensor schema refuses to let the two drift.
        homeassistant = sensor.get("homeassistant", {})

        return (
            homeassistant.get("device_class") == "energy"
            and homeassistant.get("state_class") == "total_increasing"
        )

    def is_finished_period_total(self, sensor):
        # Yesterday's, last month's and last year's generation. A step function, not a
        # counter: it holds one value until the inverter rolls it over. The sensor
        # schema allows an energy sensor no state class other than total_increasing,
        # so this and is_counter between them cover every energy register.
        homeassistant = sensor.get("homeassistant", {})

        return homeassistant.get("device_class") == "energy" and not homeassistant.get(
            "state_class"
        )

    def counters(self):
        for sensor in self.sensors_config:
            if sensor["active"] and "modbus" in sensor and self.is_counter(sensor):
                yield sensor

    def reset_period(self, sensor):
        # Which period the inverter clears this register on, None for a lifetime
        # counter that is never cleared.
        return sensor["modbus"].get("resets")

    def resetting_counters(self):
        for sensor in self.counters():
            if self.reset_period(sensor) is not None:
                yield sensor

    def period_key(self, period, date):
        return date[: PERIOD_LENGTH[period]]

    def state_topic(self, *parts):
        return "/".join(
            [self.config["mqtt"]["topic_prefix"], "_state", *[str(p) for p in parts]]
        )

    def load_state(self):
        # Which day the published counters belong to, and what each of them read before
        # the last reset, both have to survive a restart. Without the day a restart
        # would replay the midnight reset and double count everything generated earlier
        # the same day, and without the previous total the first reading of the morning
        # is unguarded.
        if not self.config["mqtt"]["enabled"]:
            self.current_day = self.local_date()
            return

        # Assume the current day when nothing is stored, so a first ever start does not
        # reset a counter that may already hold generation from earlier today.
        self.current_day = (
            self.mqtt.read_retained(self.state_topic("current_day"))
            or self.local_date()
        )
        logging.info(f"Counters belong to {self.current_day}")

        for sensor in self.counters():
            name = sensor["name"]

            if self.reset_period(sensor) is None:
                # A lifetime counter has no reset to recover from, but it must never
                # be allowed to go backwards, and the value Home Assistant is still
                # showing is the only record of where it stood before the restart.
                # The timestamp is left empty on purpose: how long the container was
                # down is unknown, so there is nothing to measure an increase against.
                published = self.published_value(sensor)

                if published is not None:
                    self.last_accepted_value[name] = (published, None)
                    logging.info(f"{name} stood at {published} before the restart")

                continue

            stored = self.mqtt.read_retained(self.state_topic(name, "previous_total"))

            if stored is None:
                continue

            self.previous_period_total[name] = float(stored)
            self.awaiting_new_period[name] = True
            logging.info(f"{name} read {stored} before the last reset")

    def reset_counters(self):
        # The inverter is asleep at midnight and only clears its own daily counter once
        # it wakes up hours later, until then it keeps reporting yesterday's total.
        # Publish the reset on the wall clock instead so the counter never reports
        # yesterday's generation as if it happened today. The same holds at a month or
        # a year boundary, which is why every counter that resets is handled here.
        today = self.local_date()

        if self.current_day == today:
            return

        for sensor in self.resetting_counters():
            period = self.reset_period(sensor)

            if not self.period_rolled_over(period, today):
                continue

            name = sensor["name"]
            logging.info(f"New {period} period, resetting {name}")

            # Whatever the counter last read is what the inverter keeps reporting until
            # it clears the register itself, so remember it to recognise it on sight.
            previous_total = self.value_before_reset(sensor)

            if previous_total is not None:
                self.previous_period_total[name] = previous_total
                self.awaiting_new_period[name] = True
                self.publish(
                    self.state_topic(name, "previous_total"),
                    previous_total,
                    retain=True,
                )

            # Move the plausibility baseline with it, the counter starts from zero now.
            self.last_accepted_value[name] = (0, monotonic())

            self.publish(
                f"{self.config['mqtt']['topic_prefix']}/{name}",
                0,
                retain=True,
            )

        self.current_day = today
        self.publish(self.state_topic("current_day"), today, retain=True)

    def period_rolled_over(self, period, today):
        # Nothing known about where the counters stand, so treat every period as
        # rolled over rather than leave a stale total in place.
        if self.current_day is None:
            return True

        return self.period_key(period, self.current_day) != self.period_key(
            period, today
        )

    def value_before_reset(self, sensor):
        name = sensor["name"]

        if name in self.last_accepted_value:
            return self.last_accepted_value[name][0]

        # Nothing accepted yet this run, so the retained topic still holds the value
        # Home Assistant is showing, which is the one the reset is about to replace.
        return self.published_value(sensor)

    def published_value(self, sensor):
        if not self.config["mqtt"]["enabled"]:
            return None

        try:
            return float(
                self.mqtt.read_retained(
                    f"{self.config['mqtt']['topic_prefix']}/{sensor['name']}"
                )
            )
        except TypeError, ValueError:
            return None

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
                                self.availability_topics(sensor),
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
                                self.availability_topics(sensor),
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
                timeout=HTTP_TIMEOUT,
            )

            mr_url = f"http://{self.config['datalogger']['host']}/moniter.cgi"
            mr = requests.get(
                mr_url,
                auth=(
                    self.config["datalogger"]["http"]["user"],
                    self.config["datalogger"]["http"]["password"],
                ),
                timeout=HTTP_TIMEOUT,
            )
        except Exception as e:
            logging.error(f"Unable to poll HTTP endpoints {e}")
            return

        if not ir.status_code == 200 or not mr.status_code == 200:
            logging.error("HTTP endpoints did not return 200")
            return

        # The sensor schema allows no endpoint other than these two, so every sensor
        # below finds its response here.
        registers = {
            "inverter": ir.text.split(";"),
            "moniter": mr.text.split(";"),
        }

        asleep = {
            endpoint
            for endpoint, fields in registers.items()
            if self.http_response_is_dead(endpoint, fields)
        }

        for sensor in self.sensors_config:
            if (
                not sensor["active"]
                or "http" not in sensor
                or "endpoint" not in sensor["http"]
                or "register" not in sensor["http"]
            ):
                continue

            endpoint = sensor["http"]["endpoint"]

            if endpoint in asleep:
                continue

            register = sensor["http"]["register"]
            value = self.http_value(registers[endpoint], endpoint, register)

            if value is not None:
                logging.info(f"{register} {sensor['description']} : {value}")
                self.publish(
                    f"{self.config['mqtt']['topic_prefix']}/{sensor['name']}",
                    value,
                    retain=True,
                )

    def http_value(self, registers, endpoint, register):
        # One field of a CGI response, or None if it does not carry a usable value.
        #
        # An http register is a position in a semicolon separated response, and the
        # only description of that response is sensors.yaml. A field the datalogger
        # does not send is therefore a missing reading, not a crash: indexing straight
        # into the list raised an IndexError that escaped query_http, query_modbus and
        # main, and took the process down with it.
        if register >= len(registers):
            logging.error(
                f"{endpoint}.cgi returned {len(registers)} fields, so there is no "
                f"register {register}"
            )
            return None

        value = registers[register].strip(HTTP_PADDING)

        if not value:
            return None

        # Padding at either end is expected and has just been removed. Anything else
        # unprintable means the field is not the string it is described as, and this
        # is the one path that puts device supplied text into a payload -- a modbus
        # reading is a number, or a status string that came from sensors.yaml. Refuse
        # it loudly rather than publish something a recorder cannot store.
        if any(character < " " for character in value):
            logging.error(
                f"Ignoring {endpoint}.cgi register {register}: {value!r} still holds "
                "a control character after the padding was stripped"
            )
            return None

        return value

    def http_registers(self, endpoint):
        # Which fields of a CGI response this app is set up to read.
        return [
            sensor["http"]["register"]
            for sensor in self.sensors_config
            if sensor["active"]
            and "http" in sensor
            and sensor["http"].get("endpoint") == endpoint
            and "register" in sensor["http"]
        ]

    def http_response_is_dead(self, endpoint, fields):
        # The datalogger is usually up before the inverter is. Its own page answers
        # properly while inverter.cgi answers with nothing at all: on the morning of
        # 2026-08-19 the serial came back empty and the firmware and model came back
        # as "000000" and "0", where awake they read "83003A" and "509". The empty
        # one was already dropped, but the other two are not empty and went straight
        # out. query_http only runs when the datalogger comes back, so those two
        # values were still what Home Assistant was showing hours later, and would
        # have stood until the next time it went away.
        #
        # This is the judgement response_is_dead makes about a block of registers,
        # for the same reason: a well formed answer in which everything reads as
        # nothing is not a reading.
        #
        # Judged only on the fields this app reads. The rest of the response is
        # described nowhere, and at least one of them holds "NO" whether the inverter
        # is awake or not, so it cannot be part of the test.
        values = [
            self.http_value(fields, endpoint, register)
            for register in self.http_registers(endpoint)
        ]
        present = [value for value in values if value is not None]

        # Every field empty is not evidence of anything: nothing would be published
        # from this endpoint either way, and http_value has already said so.
        if not present:
            return False

        # "0", "000000" and "0.0" all mean the same nothing. One zero among real
        # values is a reading -- a wifi signal really can be 0 -- so the whole set
        # has to read as nothing before the response does.
        if not all(value.strip("0.") == "" for value in present):
            return False

        logging.info(
            f"Ignoring {endpoint}.cgi, every field it is read for is empty or zero"
        )
        return True

    def datalogger_is_offline(self, *, offline: bool) -> None:
        # The state machine and nothing else: spend a retry, notice a return, record
        # the state, say so, and stand in for the readings that are gone. Each of
        # those is its own method below. They were one 61 line block called from the
        # middle of the register loop, and that mixture is what let a poll which
        # failed every chunk still leave the availability topic saying online.
        if offline and self.retry_before_declaring_offline():
            return

        if not offline:
            self.datalogger_came_back()

        self.datalogger_offline = offline

        self.publish_availability(OFFLINE if offline else ONLINE)

        if offline:
            self.publish_offline_values()

    def retry_before_declaring_offline(self) -> bool:
        # One poll the datalogger did not answer is not an offline datalogger,
        # poll_retries of them is. True while the budget lasts, and the caller then
        # leaves the state exactly as it was.
        self.datalogger_unreachable = True

        if self.retries_done >= self.config["datalogger"]["poll_retries"]:
            return False

        logging.info(
            f"Datalogger not reachable, done {self.retries_done} of "
            f"{self.config['datalogger']['poll_retries']} retries"
        )
        self.retries_done += 1
        return True

    def datalogger_came_back(self) -> None:
        # Only on the transition, and unreachable counts as away: the retry budget
        # is spent before anything is declared, so a datalogger that dropped out and
        # returned within it never reached the offline state but was gone all the
        # same. The CGI values are read here because no register carries them.
        if not (self.datalogger_offline or self.datalogger_unreachable):
            return

        self.query_http()
        self.retries_done = 0
        self.datalogger_unreachable = False

    def publish_availability(self, availability: str) -> None:
        # Only when it changed. A successful poll asserts this once for the connection
        # and again for every chunk of registers it reads, so the topic was being
        # rewritten with the answer it already held several thousand times a day. The
        # message is retained, so saying it once is saying it for good.
        if availability == self.availability_published:
            return

        self.availability_published = availability

        # The transition, both ways. Going offline was logged further down; coming
        # back was not logged at all, so the only record of it was paho's own debug
        # line for the publish. That made the one state change worth watching visible
        # only to whoever had set debug on and knew to look for a client library's
        # output, which is no way to audit a state machine.
        logging.info(f"Datalogger {availability}")

        self.publish(
            f"{self.config['mqtt']['topic_prefix']}/datalogger_availability",
            availability,
            retain=True,
        )

    def offline_value(self, sensor: dict[str, Any]) -> Any:
        # What one sensor should read while the datalogger is away, or None to leave
        # it showing what it last had. Everything not answered here is either covered
        # by the availability topic or, for an energy counter, deliberately untouched.
        if self.device_class(sensor) in OFFLINE_ZERO:
            return 0

        if "modbus" in sensor and sensor["modbus"]["read_type"] == "bit":
            return sensor["modbus"]["bit"]["default_value"]

        return None

    def publish_offline_values(self) -> None:
        for sensor in self.sensors_config:
            if not sensor["active"]:
                continue

            value = self.offline_value(sensor)

            if value is None:
                continue

            source = sensor["modbus"] if "modbus" in sensor else sensor["http"]
            logging.info(f"{source['register']} {sensor['description']} : {value}")

            self.publish(
                f"{self.config['mqtt']['topic_prefix']}/{sensor['name']}",
                value,
                retain=True,
            )

    def map_bit_to_value(
        self, mapping: dict[int, str], default_value: str, binary_string: str
    ) -> str:
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

    def get_register_interval(self) -> None:
        # Check if we already have a first and last register
        if self.register_span_start > 0 and self.register_span_end > 0:
            return

        for sensor in self.sensors_config:
            # Check if sensor is active and has a modbus read type, also check if the datalogger is online
            if not sensor["active"] or "modbus" not in sensor:
                continue

            register = sensor["modbus"]["register"]

            if self.register_span_start == 0 or register < self.register_span_start:
                self.register_span_start = register

            if register > self.register_span_end:
                # The last register of the reading, not its first. The widths live in
                # READ_TYPES, where the decoder that consumes them is, so the span
                # cannot fall behind a read type the loop already knows how to read.
                width = READ_TYPES[sensor["modbus"]["read_type"]].width

                self.register_span_end = register + width - 1

        logging.info(
            f"First register: {self.register_span_start}, Last register: {self.register_span_end}"
        )

    def value_is_publishable(self, sensor, value):
        # An energy register is either a counter, which can be checked against what
        # the inverter could have generated, or a finished period's total, which can
        # only be checked against itself. Everything else is published as it arrives.
        if self.is_counter(sensor):
            return self.value_is_plausible(sensor, value)

        if self.is_finished_period_total(sensor):
            return self.value_is_settled(sensor, value)

        return True

    def value_is_settled(self, sensor, value):
        # A plausibility check is the wrong tool here, the value legitimately jumps by
        # a whole day's generation once a day. A debounce fits: on 2026-07-28
        # generation_yesterday read 81 for almost the entire day with four one sample
        # excursions to the correct 103.2, at roughly 04:40, 09:00, 22:30 and 23:50.
        # The baseline was wrong and the spikes were right, which is inverted from
        # generation_today, but either way one sample is not a rollover.
        #
        # Nothing is restored across a restart, so the first value of a run takes a
        # few polls to appear. Home Assistant goes on showing the retained one
        # meanwhile, which is the same value in all but the case this exists for.
        name = sensor["name"]

        if value == self.settled_value.get(name):
            self.pending_value.pop(name, None)
            return True

        pending, seen = self.pending_value.get(name, (None, 0))
        seen = seen + 1 if value == pending else 1

        if seen < DEBOUNCE_POLLS:
            self.pending_value[name] = (value, seen)
            logging.info(
                f"Holding {name}: {value} seen {seen} of {DEBOUNCE_POLLS} times"
            )
            return False

        self.pending_value.pop(name, None)
        self.settled_value[name] = value
        return True

    def value_is_plausible(self, sensor, value):
        # A cumulative energy counter cannot climb faster than the inverter is able to
        # generate. The datalogger intermittently serves a value belonging to a previous
        # day, which arrives as a jump of tens of kWh between two polls and which Home
        # Assistant records as real generation. A decrease is always allowed, that is
        # what a genuine counter reset looks like.
        name = sensor["name"]
        now = monotonic()
        resolution = sensor["modbus"].get("scale", 1)

        # Until the inverter clears its own daily register it keeps reporting yesterday's
        # total verbatim, and after a night without a single successful poll the
        # allowance below has grown enough to let that through. Recognise the value
        # instead. A reading that is not yesterday's total means the inverter has moved
        # on, so stop looking for it.
        if self.awaiting_new_period.get(name):
            if abs(value - self.previous_period_total[name]) <= 2 * resolution:
                logging.warning(
                    f"Ignoring {name}: {value} is still the previous period's total, "
                    "the inverter has not reset the register yet"
                )
                return False

            self.awaiting_new_period[name] = False

        previous = self.last_accepted_value.get(name)

        if previous is None:
            self.last_accepted_value[name] = (value, now)
            return True

        last_value, last_time = previous

        if value <= last_value:
            # A counter that resets is allowed to drop, that is what the reset looks
            # like. One that never resets is not: a drop there is a fault, and taking
            # it at face value would strand the sensor, since climbing back to a
            # lifetime figure needs months of allowance to look possible.
            if value < last_value and self.reset_period(sensor) is None:
                logging.error(
                    f"Ignoring {name}: {last_value} -> {value}, a counter that never "
                    "resets cannot decrease"
                )
                return False

            self.last_accepted_value[name] = (value, now)
            return True

        if last_time is None:
            # Restored from the retained topic after a restart, with no idea how long
            # the container was down, so there is no allowance to measure against.
            # Adopt the reading and guard every one after it.
            self.last_accepted_value[name] = (value, now)
            return True

        # The resolution of the register is allowed on top of what the inverter could
        # have generated, otherwise a counter reporting whole kWh could never tick over
        # within a single poll interval.
        could_have_generated = (
            self.config["inverter"]["max_power_kw"] * (now - last_time) / 3600
        )
        allowance = could_have_generated * 1.2 + resolution

        if value - last_value > allowance:
            logging.warning(
                f"Ignoring implausible {name}: {last_value} -> {value} after "
                f"{round(now - last_time)}s, at most {round(allowance, 2)} was possible"
            )
            return False

        self.last_accepted_value[name] = (value, now)
        return True

    def response_is_dead(self, registers: dict[int, int]) -> bool:
        # The datalogger sometimes answers with a complete, well formed block of
        # registers where every single value is zero, usually while the inverter itself
        # is asleep. A live inverter cannot produce that: AC voltage alone reads about
        # 2300, and the lifetime counter is in the tens of thousands.
        if any(registers.values()):
            return False

        logging.info("Validation failed, every register in the response is zero")
        return True

    def read_chunk(
        self, client: ModbusTcpClient, address: int, count: int
    ) -> list[int] | None:
        # One chunk of registers, or None if the datalogger would not give it up.
        # Bounded attempts with a pause between them: the loop this replaces never
        # advanced and never slept, so a chunk that kept failing was retried as fast
        # as the network allowed. On the morning of 2026-07-29 single poll cycles made
        # 73, 433, 447, 405 and 473 attempts, roughly two requests a second sustained
        # for minutes, concentrated in the 05:30-05:45 window when the datalogger was
        # already struggling to stay up.
        for attempt in range(1, CHUNK_ATTEMPTS + 1):
            try:
                message = client.read_input_registers(
                    device_id=self.config["datalogger"]["device_id"],
                    address=address,
                    count=count,
                )
            except Exception as e:
                # The socket cannot be trusted after this, and pymodbus will not
                # notice: its connect() returns true whenever it still holds a socket
                # object, without testing whether anything is at the other end. So a
                # broken pipe left every later request writing into the same dead
                # socket, which is why this used to kill the process and let the
                # container restart.
                #
                # close() drops the socket, and the attempt after this one dials a
                # new connection. The poll can then still succeed, where a restart
                # lost it along with the in-memory half of the energy guards: the
                # plausibility timestamps and the debounce counts are not in the
                # retained topics load_state reads back.
                #
                # Only for a raised error. A response that says isError is the
                # datalogger declining to answer, not a dead socket -- that is what
                # it does every morning while the inverter wakes up, and reconnecting
                # each time would be pure churn.
                logging.error(f"Error occured while querying modbus: {e}")
                client.close()
            else:
                if not message.isError():
                    return message.registers

                logging.error(
                    f"Could not read registers {address} to {address + count} "
                    f"on attempt {attempt}, might have lost connection"
                )

            if attempt < CHUNK_ATTEMPTS:
                sleep(CHUNK_RETRY_DELAY)

        return None

    def connect_to_datalogger(self) -> ModbusTcpClient | None:
        # A connected client, or None with the datalogger already told it is offline.
        try:
            client = ModbusTcpClient(
                self.config["datalogger"]["host"],
                port=self.config["datalogger"]["port"],
                timeout=MODBUS_TIMEOUT,
                # One attempt per request. read_chunk owns the retrying.
                retries=0,
            )

            if not client.connect():
                raise ModbusException("Client not connected to datalogger")

        except Exception as e:
            logging.error(f"Unable to connect to datalogger: {e}")
            if not self.datalogger_offline:
                self.datalogger_is_offline(offline=True)
            return None

        # A connection that opens says nothing about whether there is a working
        # datalogger behind it, so nothing is declared here. Every morning while the
        # inverter wakes up, this one accepts the connection and then refuses to serve
        # a single register, and saying "online" on the strength of the handshake
        # reset retries_done on every one of those polls. The counter never reached
        # poll_retries, the datalogger was never declared offline, and the availability
        # topic sat at online for as long as the state lasted -- thirteen consecutive
        # failed polls on the morning of 2026-08-19, about seven minutes, with Home
        # Assistant holding the voltages and the frequency from before it started.
        #
        # The first chunk of registers that actually arrives is what says we are
        # online, in read_span below.
        return client

    def read_span(self, client: ModbusTcpClient) -> tuple[dict[int, int], int]:
        # The whole register span, chunk by chunk, with the number of registers that
        # were asked for. The two together are what the caller judges the poll on: a
        # short answer is a failed poll, however well formed each chunk was.
        registers: dict[int, int] = {}
        chunk_size = self.config["datalogger"]["register_chunks"]
        expected_registers = 0

        # register_span_end is the last register wanted, not one past it, so the range
        # has to reach it. It did not, and a chunk size that happened to land exactly
        # on the end silently lost that register: with the span 3004 to 3077 a
        # register_chunks of 73 read 3004 to 3076 and stopped. The length check below
        # passed, because 73 were asked for and 73 arrived, so the poll was kept and
        # only the sensor needing the missing register failed -- as a KeyError caught
        # up in main, logged once a poll, forever.
        #
        # The count is clamped for the same reason the range moved. Reading whole
        # chunks past the end covered that bug by accident, six registers past 3077 at
        # the default chunk size, and asking a datalogger this fragile for registers
        # nothing is mapped to is not a habit worth keeping.
        for address in range(
            self.register_span_start, self.register_span_end + 1, chunk_size
        ):
            count = min(chunk_size, self.register_span_end - address + 1)

            logging.info(f"Querying register {address} to {address + count - 1}")

            # Count each chunk once, not once per attempt. Counting per attempt
            # inflated the expected total to thousands and threw away five complete
            # 80 register responses on 2026-07-29 alone.
            expected_registers += count

            values = self.read_chunk(client, address, count)

            if values is None:
                # Nothing to gain from asking a datalogger that just refused three
                # times for the rest of the span, and the length check on the way out
                # will discard this poll anyway.
                if not self.datalogger_offline:
                    self.datalogger_is_offline(offline=True)
                break

            logging.info(f"Result: {values}")
            registers.update(dict(enumerate(values, start=address)))

            self.datalogger_is_offline(offline=False)

        return registers, expected_registers

    def query_modbus(self) -> dict[int, int]:
        logging.info("Querying modbus")

        client = self.connect_to_datalogger()

        if client is None:
            return {}

        registers, expected_registers = self.read_span(client)

        client.close()

        # Sometimes we get a response with almost all values being 0, usually also multiple registers
        # are missing. In that case we just return an empty dictionary. This validation is not perfect
        # but it should be good enough for now.
        if len(registers) != expected_registers:
            logging.info(
                f"Validation of number of queried registers failed. "
                f"Queried: {expected_registers}, received: {len(registers)}"
            )
            return {}

        if self.response_is_dead(registers):
            return {}

        return registers

    def pick_from_registers(
        self, registers: dict[int, int], start: int, count: int
    ) -> list[int]:
        return [registers[i] for i in range(start, start + count)]

    def seconds_until_next_poll(self, started: float) -> float:
        # The interval is the gap between the starts of two polls, not the gap
        # between the end of one and the start of the next. Sleeping the whole
        # interval after the work added the length of the poll to every cycle, which
        # is a second or two normally and much more when a chunk had to be retried:
        # the poll that gave up on the datalogger took longer than the interval it
        # was then followed by.
        #
        # A poll that overran its own interval is not made up for, it just starts the
        # next one immediately. Measuring from the start each time means a slow poll
        # cannot leave a debt behind for the ones after it.
        interval = (
            self.config["datalogger"]["poll_interval"]
            if not self.datalogger_offline
            else self.config["datalogger"]["poll_interval_if_off"]
        )

        return max(0, interval - (monotonic() - started))

    def decode_register(self, sensor: dict[str, Any], values: list[int]) -> Any:
        value = values[0]

        if "scale" in sensor["modbus"]:
            value = float(value) * sensor["modbus"]["scale"]

            if "decimals" in sensor["modbus"]:
                value = round(value, sensor["modbus"]["decimals"])

        return value

    def decode_long(self, sensor: dict[str, Any], values: list[int]) -> Any:
        return ModbusTcpClient.convert_from_registers(
            values,
            data_type=ModbusTcpClient.DATATYPE.INT32,
        )

    def decode_composed_datetime(
        self, sensor: dict[str, Any], values: list[int]
    ) -> Any:
        # The inverter's own clock, six registers of two digit numbers. Nothing is
        # derived from it: docs/energy-guards.md records why it cannot be trusted to
        # say which day a reading belongs to.
        return (
            f"20{values[0]:02d}-{values[1]:02d}-{values[2]:02d}"
            f"T{values[3]:02d}:{values[4]:02d}:{values[5]:02d}{self.timezone_offset}"
        )

    def decode_alarm(self, sensor: dict[str, Any], values: list[int]) -> Any:
        return "ON" if any(values) else "OFF"

    def decode_bit(self, sensor: dict[str, Any], values: list[int]) -> Any:
        value = values[0]

        if "bit" not in sensor["modbus"] or "map" not in sensor["modbus"]["bit"]:
            logging.error("Could not find needed modbus.bit.map config")
            return None

        return self.map_bit_to_value(
            sensor["modbus"]["bit"]["map"],
            sensor["modbus"]["bit"]["default_value"],
            bin(value),
        )

    def decode_sensor(self, sensor: dict[str, Any], registers: dict[int, int]) -> Any:
        # One sensor's reading out of the registers this poll returned, or None if it
        # could not be made. This whole chain used to sit inline in the poll loop, so
        # a decode could not be exercised without running a poll, and a new read type
        # meant another branch in the middle of it.
        try:
            # Verify that we have the register needed
            if sensor["modbus"]["register"] not in registers:
                raise Exception(f"Register {sensor['modbus']['register']} not found")

            read_type = READ_TYPES.get(sensor["modbus"]["read_type"])

            if read_type is None:
                logging.error(
                    f"modbus.readtype of {sensor['modbus']['read_type']} not supported"
                )
                return None

            values = self.pick_from_registers(
                registers,
                sensor["modbus"]["register"],
                read_type.width,
            )

            return read_type.decode(self, sensor, values)

        except Exception as e:
            logging.error(f"Error occured {e}")
            return None

    def publish_readings(self, registers: dict[int, int]) -> None:
        for sensor in self.sensors_config:
            # Check if sensor is active and has a modbus read type
            if (
                not sensor["active"]
                or "modbus" not in sensor
                or "read_type" not in sensor["modbus"]
            ):
                continue

            value = self.decode_sensor(sensor, registers)

            # None is not a reading. Nothing publishable decodes to it, so it is free
            # to mean "this one could not be read", which is what the log lines in
            # decode_sensor have already said.
            if value is None:
                continue

            if not self.value_is_publishable(sensor, value):
                continue

            logging.info("Publishing sensor %s: %s", sensor["name"], value)

            self.publish(
                f"{self.config['mqtt']['topic_prefix']}/{sensor['name']}",
                value,
                retain=True,
            )

    def main(self) -> None:
        # Generate Home assistant MQTT discovery topics
        self.generate_ha_discovery_topics()

        # Get register interval, find lowest and higest register numbers
        self.get_register_interval()

        # Find out which day the counters currently published to MQTT belong to
        self.load_state()

        while True:
            logging.debug("Datalogger scan start at " + datetime.now().isoformat())
            poll_started = monotonic()

            # Reset the counters on the wall clock, the datalogger is unreachable
            # at midnight so this cannot wait for a successful poll
            self.reset_counters()

            registers = self.query_modbus()

            # An empty answer is a failed poll, and a failed poll publishes nothing:
            # query_modbus has already dealt with the availability topic and with
            # whatever has to be said on the sensors themselves.
            if registers:
                self.publish_readings(registers)

            # Wait until the next poll is due, which is the configured interval after
            # this one started, or the longer interval if the datalogger is not
            # answering.
            sleep_duration = self.seconds_until_next_poll(poll_started)

            logging.debug(f"Datalogger scanning paused for {sleep_duration} seconds")
            sleep(sleep_duration)


# Every modbus read type there is, and the only place any of them is named. The poll
# loop and the polled register span both come from this table, so a new read type is
# an entry here and a decoder above rather than another branch in each of them. The
# names are the ones sensors.yaml is allowed to use; app/sensors.py is the gate.
READ_TYPES = {
    "register": ReadType(1, App.decode_register),
    "long": ReadType(2, App.decode_long),
    "composed_datetime": ReadType(6, App.decode_composed_datetime),
    "alarm": ReadType(4, App.decode_alarm),
    "bit": ReadType(1, App.decode_bit),
}


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
