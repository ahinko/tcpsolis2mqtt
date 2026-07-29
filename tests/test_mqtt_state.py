"""Retained topics are the only storage this app has, so they double as the place
where the daily counter state survives a restart.

These tests run against a real broker rather than a mock, because the behaviour
being relied on is the broker's: a retained message is delivered on subscribe, and
an empty topic is only detectable by timing out.
"""

import asyncio
import logging
import threading
from functools import partial
from time import monotonic, sleep

import pytest

pytest.importorskip("amqtt", reason="amqtt provides the in-process test broker")

from amqtt.broker import Broker  # noqa: E402
from app import App  # noqa: E402
from mqtt import Mqtt  # noqa: E402

PORT = 11883
PREFIX = "tcpsolis2mqtt"
VALUE_TOPIC = f"{PREFIX}/generation_today"
LIFETIME_TOPIC = f"{PREFIX}/total_power"
AVAILABILITY_TOPIC = f"{PREFIX}/availability"


@pytest.fixture(scope="session")
def broker():
    ready = threading.Event()
    holder = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def start():
            instance = Broker(
                {
                    "listeners": {
                        "default": {"type": "tcp", "bind": f"127.0.0.1:{PORT}"}
                    },
                    "sys_interval": 0,
                    "auth": {"allow-anonymous": True},
                }
            )
            await instance.start()
            return instance

        # Keep a reference so the broker is not garbage collected.
        holder["broker"] = loop.run_until_complete(start())
        holder["loop"] = loop
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    assert ready.wait(30), "test broker did not start"

    yield

    holder["loop"].call_soon_threadsafe(holder["loop"].stop)


@pytest.fixture
def mqtt_config():
    return {
        "enabled": True,
        "client_id": "test",
        "user": None,
        "password": None,
        "use_ssl": False,
        "validate_cert": False,
        "host": "127.0.0.1",
        "port": PORT,
        "topic_prefix": PREFIX,
    }


@pytest.fixture
def connect(broker, mqtt_config):
    """Connect a client, named uniquely so tests never share a session."""
    clients = []
    counter = [0]

    def _connect():
        counter[0] += 1
        client = Mqtt({**mqtt_config, "client_id": f"test-{id(clients)}-{counter[0]}"})
        assert client.wait_until_connected(10), "client did not connect"
        clients.append(client)
        return client

    yield _connect

    for client in clients:
        client.loop_stop()
        client.disconnect()


@pytest.fixture
def store(connect):
    """Write a retained topic and wait for the broker to have processed it."""
    client = connect()

    def _store(topic, payload):
        # QoS 1, so waiting means the broker acknowledged rather than merely that
        # the bytes reached the socket. Without this the reads below race.
        client.publish(topic, payload, qos=1, retain=True).wait_for_publish(10)

    return _store


@pytest.fixture
def clean(store):
    for topic in [
        VALUE_TOPIC,
        LIFETIME_TOPIC,
        AVAILABILITY_TOPIC,
        f"{PREFIX}/_state/current_day",
        f"{PREFIX}/_state/generation_today/previous_total",
        f"{PREFIX}/_state/energy_this_month/previous_total",
        f"{PREFIX}/_state/generation_this_year/previous_total",
    ]:
        store(topic, None)


def build_app(client, mqtt_config, sensors_config, day):
    app = App.__new__(App)
    app.config = {"mqtt": mqtt_config, "inverter": {"max_power_kw": 15}}
    app.sensors_config = sensors_config
    app.last_accepted_value = {}
    app.current_day = None
    app.previous_period_total = {}
    app.awaiting_new_period = {}
    # An empty topic is only detectable by timing out and load_state reads up to five
    # of them, so the default five seconds each dominates the whole suite. One second
    # is generous against a broker in this same process.
    app.mqtt = client
    app.mqtt.read_retained = partial(Mqtt.read_retained, client, timeout=1)
    app.local_date = lambda: day
    return app


def wait_for(condition, timeout=5):
    deadline = monotonic() + timeout

    while not condition() and monotonic() < deadline:
        sleep(0.05)

    return condition()


def test_the_connect_callback_fires(broker, mqtt_config, caplog):
    caplog.set_level(logging.INFO)
    client = Mqtt({**mqtt_config, "client_id": "callback-test"})

    try:
        assert client.wait_until_connected(10), "client did not connect"
        # It runs on the network thread, a moment after is_connected flips.
        assert wait_for(lambda: "MQTT Connected to Broker!" in caplog.text)
    finally:
        client.loop_stop()
        client.disconnect()


def test_online_is_announced_on_connect(connect, store, clean):
    # clean wipes the topic, so anything read back was published by connecting.
    assert connect().read_retained(AVAILABILITY_TOPIC, timeout=2) == "online"


def test_the_will_marks_the_app_offline(connect):
    # The broker publishes this when the connection drops without a clean disconnect,
    # which is the only thing that stops a crashed container from looking alive.
    client = connect()

    assert client._will_topic == AVAILABILITY_TOPIC.encode()
    assert client._will_payload == b"offline"
    assert client._will_retain


def test_a_deliberate_disconnect_stays_disconnected(broker, mqtt_config, caplog):
    # The disconnect handler used to run a manual reconnect loop, twelve attempts
    # with exponential backoff, on paho's own network thread. It was believed to be
    # dead code, on the grounds that a method named on_disconnect shadows paho's
    # property of the same name so the property setter never runs. It does never run,
    # _on_disconnect really does stay None, but paho reads the public attribute
    # rather than the private one, so the handler fired and the loop was live: a
    # disconnect was logged and then immediately undone, "MQTT Reconnecting in 1
    # seconds" followed by "MQTT Reconnected successfully!".
    caplog.set_level(logging.INFO)
    client = Mqtt({**mqtt_config, "client_id": "disconnect-test"})

    try:
        assert client.wait_until_connected(10), "client did not connect"
        client.disconnect()

        assert wait_for(lambda: not client.is_connected())
        assert "Reconnecting" not in caplog.text
        assert not wait_for(lambda: client.is_connected(), timeout=2)
    finally:
        client.loop_stop()


def test_missing_retained_topic_reads_as_none(connect, clean):
    client = connect()

    assert client.read_retained(f"{PREFIX}/_state/current_day", timeout=2) is None


def test_retained_topic_is_readable_by_a_new_client(connect, store, clean):
    store(f"{PREFIX}/_state/current_day", "2026-07-28")

    assert connect().read_retained(f"{PREFIX}/_state/current_day") == "2026-07-28"


def test_reading_twice_works(connect, store, clean):
    store(f"{PREFIX}/_state/current_day", "2026-07-28")
    client = connect()

    assert client.read_retained(f"{PREFIX}/_state/current_day") == "2026-07-28"
    assert client.read_retained(f"{PREFIX}/_state/current_day") == "2026-07-28"


def test_publishing_still_works_after_a_read(connect, store, clean):
    client = connect()
    client.read_retained(f"{PREFIX}/_state/current_day", timeout=2)

    result = client.publish(VALUE_TOPIC, "0", retain=True)
    result.wait_for_publish(10)

    assert result.is_published()


def test_state_is_restored_after_a_restart(
    connect, store, clean, mqtt_config, sensors_config
):
    store(f"{PREFIX}/_state/current_day", "2026-07-29")
    store(f"{PREFIX}/_state/generation_today/previous_total", "40.0")

    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")
    app.load_state()

    assert app.current_day == "2026-07-29"
    assert app.previous_period_total["generation_today"] == 40.0
    assert app.awaiting_new_period["generation_today"] is True


def test_first_ever_start_assumes_today(connect, clean, mqtt_config, sensors_config):
    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")
    app.load_state()

    # Nothing stored, so the reset must not fire against a counter that may already
    # hold generation from earlier today.
    assert app.current_day == "2026-07-29"
    assert app.previous_period_total == {}


def test_previous_total_falls_back_to_the_published_value(
    connect, store, clean, mqtt_config, sensors_config, generation_today
):
    # Restarted while down overnight: nothing accepted yet this run, so the value
    # Home Assistant is showing is the one the reset is about to replace.
    store(VALUE_TOPIC, "40.0")

    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")

    assert app.value_before_reset(generation_today) == 40.0


def test_the_lifetime_counter_floor_survives_a_restart(
    connect, store, clean, mqtt_config, sensors_config
):
    # total_power never resets, so nothing lower than what Home Assistant is already
    # showing may be published over it. Without restoring the floor the first reading
    # of the run would become the baseline, and a bogus 0 would strand the sensor.
    store(LIFETIME_TOPIC, "39901")

    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")
    app.load_state()

    assert app.last_accepted_value["total_power"] == (39901.0, None)


def test_a_first_ever_start_has_no_lifetime_floor(
    connect, clean, mqtt_config, sensors_config
):
    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")
    app.load_state()

    assert "total_power" not in app.last_accepted_value


def test_previous_total_is_none_when_nothing_was_ever_published(
    connect, clean, mqtt_config, sensors_config, generation_today
):
    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")

    assert app.value_before_reset(generation_today) is None
