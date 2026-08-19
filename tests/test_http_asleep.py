"""What the CGIs say while the inverter is still asleep.

The datalogger is usually up before the inverter is. Its own page answers properly;
the inverter's page answers with nothing.

On the morning of 2026-08-19 the app read inverter.cgi thirteen times between 05:32
and 05:39 and published `Inverter Firmware : 000000` and `Inverter Model : 0` every
time, where the awake values are 83003A and 509. The serial came back empty and was
already dropped, so it kept the good value by luck. The other two are not empty, so
nothing stopped them, and because query_http only runs when the datalogger comes
back they were still what Home Assistant was showing hours later.

The awake response below is the one captured on 2026-08-15. The asleep one is
reconstructed: the log records the three fields the app reads, not the whole body.
"""

import pytest

import app as app_module

RESPONSE_LENGTH = 1313
LEADING_PADDING = 33


def response(fields):
    return ("\x00" * LEADING_PADDING + fields).ljust(RESPONSE_LENGTH, "\x00")


INVERTER_AWAKE = response(
    "1805090232050086;83003A;509;29.2;240;50.599998;41298;NO;\r\n"
)

# Fields 0, 1 and 2 are the ones read, and are what the log proves. "NO" in field 7
# is the awake value kept deliberately: something in this response is non-zero even
# when the inverter is asleep, which is why the check cannot look at the whole body.
INVERTER_ASLEEP = response(";000000;0;0.0;0;0.0;0;NO;\r\n")

MONITER = response(
    ";3;7A123208131058AB;10010125;Disabled;null;null;Enabled;Airthings;24;"
    "192.168.71.135;92:78:48:DA:27:75;Connected;null;"
)

# Field 9 is the wifi signal. A real 0 there must not condemn the whole response.
MONITER_NO_SIGNAL = response(
    ";3;7A123208131058AB;10010125;Disabled;null;null;Enabled;Airthings;0;"
    "192.168.71.135;92:78:48:DA:27:75;Connected;null;"
)


@pytest.fixture
def http_app(make_app, monkeypatch):
    def _make(inverter=INVERTER_AWAKE, moniter=MONITER):
        app = make_app()
        app.config["datalogger"]["http"] = {
            "enabled": True,
            "user": "admin",
            "password": "123456789",
        }

        bodies = {"inverter.cgi": inverter, "moniter.cgi": moniter}

        class Response:
            def __init__(self, text):
                self.text = text
                self.status_code = 200

        monkeypatch.setattr(
            app_module.requests,
            "get",
            lambda url, **kwargs: Response(bodies[url.rsplit("/", 1)[-1]]),
        )

        return app

    return _make


def published(app):
    return {topic.rsplit("/", 1)[-1]: payload for topic, payload in app.published}


def test_an_awake_inverter_publishes_what_it_says(http_app):
    app = http_app()
    app.query_http()

    assert published(app)["inverter_firmware"] == "83003A"
    assert published(app)["inverter_model"] == "509"


def test_an_asleep_inverter_publishes_nothing_at_all(http_app):
    # The regression. "000000" and "0" used to go straight out and stand all day.
    app = http_app(inverter=INVERTER_ASLEEP)
    app.query_http()

    assert "inverter_firmware" not in published(app)
    assert "inverter_model" not in published(app)
    assert "inverter_serial" not in published(app)


def test_the_dataloggers_own_page_is_judged_separately(http_app):
    # It was answering correctly all through that morning, and there is no reason to
    # lose the wifi and datalogger sensors because the inverter has not woken.
    app = http_app(inverter=INVERTER_ASLEEP)
    app.query_http()

    assert published(app)["datalogger_serial"] == "7A123208131058AB"
    assert published(app)["wifi_ssid"] == "Airthings"


def test_one_zero_among_real_values_is_still_a_reading(http_app):
    # A wifi signal really can be 0. The response is only dead when every field the
    # app reads from it is.
    app = http_app(moniter=MONITER_NO_SIGNAL)
    app.query_http()

    assert published(app)["wifi_signal"] == "0"
    assert published(app)["datalogger_serial"] == "7A123208131058AB"


def test_a_zeroed_response_does_not_overwrite_what_was_published_before(http_app):
    # The shape of the actual incident: a good read, then the datalogger comes back
    # while the inverter is still asleep and the second read has to leave it alone.
    app = http_app()
    app.query_http()

    app = http_app(inverter=INVERTER_ASLEEP)
    app.query_http()

    assert "inverter_firmware" not in published(app)
