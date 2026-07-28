"""Retained topics are the only storage this app has, so they double as the place
where the daily counter state survives a restart.

These tests run against a real broker rather than a mock, because the behaviour
being relied on is the broker's: a retained message is delivered on subscribe, and
an empty topic is only detectable by timing out.
"""

import asyncio
import threading

import pytest

pytest.importorskip("amqtt", reason="amqtt provides the in-process test broker")

from amqtt.broker import Broker  # noqa: E402
from app import App  # noqa: E402
from mqtt import Mqtt  # noqa: E402

PORT = 11883
PREFIX = "tcpsolis2mqtt"
VALUE_TOPIC = f"{PREFIX}/generation_today"


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
        f"{PREFIX}/_state/current_day",
        f"{PREFIX}/_state/generation_today/previous_total",
    ]:
        store(topic, None)


def build_app(client, mqtt_config, sensors_config, day):
    app = App.__new__(App)
    app.config = {"mqtt": mqtt_config, "inverter": {"max_power_kw": 15}}
    app.sensors_config = sensors_config
    app.last_accepted_value = {}
    app.current_day = None
    app.previous_day_total = {}
    app.awaiting_new_day = {}
    app.mqtt = client
    app.local_date = lambda: day
    return app


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
    assert app.previous_day_total["generation_today"] == 40.0
    assert app.awaiting_new_day["generation_today"] is True


def test_first_ever_start_assumes_today(connect, clean, mqtt_config, sensors_config):
    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")
    app.load_state()

    # Nothing stored, so the reset must not fire against a counter that may already
    # hold generation from earlier today.
    assert app.current_day == "2026-07-29"
    assert app.previous_day_total == {}


def test_previous_total_falls_back_to_the_published_value(
    connect, store, clean, mqtt_config, sensors_config, generation_today
):
    # Restarted while down overnight: nothing accepted yet this run, so the value
    # Home Assistant is showing is the one the reset is about to replace.
    store(VALUE_TOPIC, "40.0")

    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")

    assert app.value_before_reset(generation_today) == 40.0


def test_previous_total_is_none_when_nothing_was_ever_published(
    connect, clean, mqtt_config, sensors_config, generation_today
):
    app = build_app(connect(), mqtt_config, sensors_config, "2026-07-29")

    assert app.value_before_reset(generation_today) is None
