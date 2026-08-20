import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "app"))

import app as app_module  # noqa: E402
from app import App  # noqa: E402
from sensors import Sensor  # noqa: E402


@pytest.fixture(scope="session")
def sensors_config():
    with open(REPO_ROOT / "sensors.yaml") as file:
        return Sensor(many=True).load(yaml.safe_load(file))


@pytest.fixture
def generation_today(sensors_config):
    return next(s for s in sensors_config if s["name"] == "generation_today")


@pytest.fixture
def energy_this_month(sensors_config):
    return next(s for s in sensors_config if s["name"] == "energy_this_month")


@pytest.fixture
def generation_this_year(sensors_config):
    return next(s for s in sensors_config if s["name"] == "generation_this_year")


@pytest.fixture
def total_power(sensors_config):
    return next(s for s in sensors_config if s["name"] == "total_power")


# The registers sensors.yaml actually asks for: 3004 to 3077 inclusive.
SPAN = 74
FIRST, LAST = 3004, 3077


class Response:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self.error = error

    def isError(self):
        return self.error


class FakeSocket:
    """Records the socket options the app sets, and can refuse them."""

    def __init__(self, refuse=False):
        self.options = []
        self.refuse = refuse

    def setsockopt(self, level, option, value):
        if self.refuse:
            raise OSError("Protocol not available")

        self.options.append((level, option, value))


class StubClient:
    """A datalogger that answers with whatever the test queued up.

    One object stands in for every client the app builds, so that dialling again
    shows up as another connect() rather than as another object.
    """

    def __init__(
        self, *answers, refuse_socket_options=False, hangs_up_after_read=False
    ):
        self.answers = list(answers)
        self.reads = []
        self.closes = 0
        self.connects = 0
        self.connected = False
        self.socket = None
        # Every socket this client has had, since close() drops the current one and a
        # test still wants to see what was set on it.
        self.sockets = []
        self.refuse_socket_options = refuse_socket_options
        # The datalogger hanging up on its own: pymodbus closes its socket when the
        # peer goes away mid-read and says nothing if the answer arrived first.
        self.hangs_up_after_read = hangs_up_after_read

    def connect(self):
        self.connects += 1
        self.connected = True
        self.socket = FakeSocket(refuse=self.refuse_socket_options)
        self.sockets.append(self.socket)
        return True

    def close(self):
        self.closes += 1
        self.connected = False
        self.socket = None

    def read_input_registers(self, device_id, address, count):
        self.reads.append((address, count))
        answer = self.answers.pop(0) if self.answers else Response(error=True)

        if isinstance(answer, Exception):
            raise answer

        if self.hangs_up_after_read:
            self.connected = False
            self.socket = None

        return answer


def live_response(address=FIRST, count=SPAN):
    # Register 3041 is the inverter temperature, the only thing that has to be non
    # zero for the response not to count as dead.
    return Response([250 if address + i == 3041 else 0 for i in range(count)])


class Clock:
    """Stand in for time.monotonic so tests can span days in an instant."""

    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(app_module, "monotonic", lambda: clock.now)
    monkeypatch.setattr(app_module, "sleep", clock.advance)
    return clock


@pytest.fixture
def make_app(sensors_config):
    """Build an App with MQTT disabled, so no broker is needed.

    Published messages are recorded instead of sent, which is what the daily reset
    assertions look at.
    """

    def _make(
        day="2026-07-28",
        max_power_kw=15,
        poll_retries=3,
        register_chunks=80,
        persistent_connection=False,
    ):
        app = App.__new__(App)
        app.config = {
            "mqtt": {"enabled": False, "topic_prefix": "tcpsolis2mqtt"},
            "inverter": {"max_power_kw": max_power_kw},
            "datalogger": {
                "host": "192.0.2.1",
                "port": 502,
                "device_id": 1,
                "poll_interval": 30,
                "poll_interval_if_off": 600,
                "poll_retries": poll_retries,
                "register_chunks": register_chunks,
                "persistent_connection": persistent_connection,
                "http": {"enabled": False},
            },
        }
        app.sensors_config = sensors_config
        app.last_accepted_value = {}
        app.current_day = None
        app.previous_period_total = {}
        app.awaiting_new_period = {}
        app.settled_value = {}
        app.pending_value = {}

        app.datalogger_offline = False
        app.datalogger_unreachable = True
        app.availability_published = None
        app.retries_done = 0
        app.register_span_start = 0
        app.register_span_end = 0

        app.client = None
        app.connections_opened = 0
        app.connection_closed_reason = "nothing has been connected yet"

        app.day = day
        app.local_date = lambda: app.day

        app.published = []
        app.publish = lambda topic, payload, retain=False: app.published.append(
            (topic, payload)
        )

        return app

    return _make


@pytest.fixture
def query(make_app, clock, monkeypatch):
    """Run one poll against a stub datalogger and return what it produced."""

    def _query(*answers, client=None, **config):
        client = client or StubClient(*answers)
        app = make_app(**config)
        app.get_register_interval()

        def build(*args, **kwargs):
            app.client_kwargs = kwargs
            return client

        monkeypatch.setattr(app_module, "ModbusTcpClient", build)
        return app, client, app.query_modbus()

    return _query


@pytest.fixture
def polls(make_app, clock, monkeypatch):
    """One App polled repeatedly, so state that carries between polls is visible."""

    def _polls(*rounds, client=None, **config):
        app = make_app(**config)
        app.get_register_interval()
        client = client or StubClient()
        app.stub_client = client

        def build(*args, **kwargs):
            return client

        monkeypatch.setattr(app_module, "ModbusTcpClient", build)

        for answers in rounds:
            client.answers = list(answers)
            app.query_modbus()

        return app

    return _polls
