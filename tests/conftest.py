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

    def _make(day="2026-07-28", max_power_kw=15, poll_retries=3, register_chunks=80):
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
        app.retries_done = 0
        app.register_span_start = 0
        app.register_span_end = 0

        app.day = day
        app.local_date = lambda: app.day

        app.published = []
        app.publish = lambda topic, payload, retain=False: app.published.append(
            (topic, payload)
        )

        return app

    return _make
