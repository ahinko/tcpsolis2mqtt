"""How often the CGI pages are read.

Coming back online used to be the only thing that read them, which put the one
scrape of the day at the worst possible moment. The datalogger is up before the
inverter is, so inverter.cgi answers with zeros, http_response_is_dead drops the
whole endpoint -- correctly -- and nothing tried again until the next time the
datalogger went away. On 2026-08-25 that scrape landed at 05:56 and happened to
catch real values; the morning of 2026-08-19 in test_http_asleep.py is the same
moment going the other way, and those sensors sat empty in Home Assistant all day.

Four times a day is aimed at that rather than at freshness. Nothing on these pages
moves quickly except the wifi signal.
"""

import pytest

import app as app_module
from app import HTTP_REFRESH_SECONDS

RESPONSE_LENGTH = 1313
LEADING_PADDING = 33


def response(fields):
    return ("\x00" * LEADING_PADDING + fields).ljust(RESPONSE_LENGTH, "\x00")


INVERTER_AWAKE = response(
    "1805090232050086;83003A;509;29.2;240;50.599998;41298;NO;\r\n"
)
INVERTER_ASLEEP = response(";000000;0;0.0;0;0.0;0;NO;\r\n")
MONITER = response(
    ";3;7A123208131058AB;10010125;Disabled;null;null;Enabled;Airthings;24;"
    "192.168.71.135;92:78:48:DA:27:75;Connected;null;"
)


@pytest.fixture
def http_app(make_app, clock, monkeypatch):
    """An app whose CGI fetches are counted, and whose inverter page can change."""

    def _make(*inverter_bodies, enabled=True):
        app = make_app()
        app.config["datalogger"]["http"] = {
            "enabled": enabled,
            "user": "admin",
            "password": "123456789",
        }
        app.fetches = []
        bodies = list(inverter_bodies) or [INVERTER_AWAKE]

        class Response:
            def __init__(self, text):
                self.text = text
                self.status_code = 200

        def get(url, **kwargs):
            page = url.rsplit("/", 1)[-1]
            if page == "inverter.cgi":
                app.fetches.append(page)
                # The last body stands once the queued ones are used up.
                return Response(bodies[min(len(app.fetches) - 1, len(bodies) - 1)])
            return Response(MONITER)

        monkeypatch.setattr(app_module.requests, "get", get)
        return app

    return _make


def published(app):
    return {topic.rsplit("/", 1)[-1]: payload for topic, payload in app.published}


def test_the_first_refresh_reads_the_pages(http_app):
    app = http_app()
    app.refresh_http_if_due()

    assert app.fetches == ["inverter.cgi"]


def test_a_second_refresh_straight_after_reads_nothing(http_app):
    app = http_app()
    app.refresh_http_if_due()
    app.refresh_http_if_due()

    assert len(app.fetches) == 1, "the values have not stood long enough"


def test_the_pages_are_read_again_once_the_values_have_stood(http_app, clock):
    app = http_app()
    app.refresh_http_if_due()
    clock.advance(HTTP_REFRESH_SECONDS)
    app.refresh_http_if_due()

    assert len(app.fetches) == 2


def test_the_scrape_on_coming_back_starts_the_clock(http_app):
    # datalogger_came_back reads the pages itself, and that has to count as a read or
    # the refresh in the poll right behind it would fetch the same values twice.
    app = http_app()
    app.datalogger_came_back()
    app.refresh_http_if_due()

    assert len(app.fetches) == 1


def test_a_scrape_that_found_an_asleep_inverter_gets_another_go(http_app, clock):
    # The bug this exists for. The first read lands while the inverter is still
    # asleep, so the whole endpoint is dropped and inverter_firmware is never
    # published. Before the timer, that was the end of it until tomorrow.
    app = http_app(INVERTER_ASLEEP, INVERTER_AWAKE)
    app.refresh_http_if_due()

    assert "inverter_firmware" not in published(app)

    clock.advance(HTTP_REFRESH_SECONDS)
    app.refresh_http_if_due()

    assert published(app)["inverter_firmware"] == "83003A"


def test_the_dataloggers_own_page_was_never_the_problem(http_app):
    # It answers while the inverter sleeps, so these arrive on the first read either
    # way. The refresh is what keeps the wifi signal from standing for a whole day.
    app = http_app(INVERTER_ASLEEP)
    app.refresh_http_if_due()

    assert published(app)["wifi_signal"] == "24"


def test_http_switched_off_does_not_stay_permanently_due(http_app):
    # query_http returns early when it is disabled. If that skipped the stamp, every
    # poll for the rest of the run would find the refresh due and call it again.
    app = http_app(enabled=False)
    app.refresh_http_if_due()

    assert app.http_polled_at is not None
    assert app.fetches == []
