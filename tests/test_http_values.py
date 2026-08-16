"""The datalogger pads its CGI responses with NUL bytes.

Both CGIs answer with a fixed 1313 byte buffer: 33 NUL bytes, then the semicolon
separated fields, then NUL again to the end of the buffer. So the first field of a
response carries 33 leading NULs and the last one is padding to the end.

tcpsolis2mqtt/inverter_serial published a 49 character payload of which 33 were NUL.
Home Assistant stores the payload verbatim and its recorder cannot INSERT a NUL into
a Postgres text column, which fails the flush and discards every other row queued in
the same commit.

It was the only topic affected because inverter_serial is the only sensor in
sensors.yaml mapped to field 0 of either response. That makes it a property of the
register map rather than of that one sensor, so the fields nothing is mapped to are
covered here too.

The two responses below are the real ones, captured 2026-08-15 with the inverter
awake.
"""

import pytest

import app as app_module

RESPONSE_LENGTH = 1313
LEADING_PADDING = 33


def response(fields):
    return ("\x00" * LEADING_PADDING + fields).ljust(RESPONSE_LENGTH, "\x00")


INVERTER_SERIAL = "1805090232050086"
DATALOGGER_SERIAL = "7A123208131058AB"

# Fields 0, 1 and 2 are read as inverter_serial, inverter_firmware and inverter_model.
# Field 2 reads "509" on this hardware, which is what the datalogger's own web UI
# shows against "Inverter model" -- odd for a model designation, but the label is the
# vendor's and not this project's invention.
INVERTER_CGI = response(
    f"{INVERTER_SERIAL};83003A;509;29.2;240;50.599998;41298;NO;\r\n"
)

# Read at fields 2, 3, 8, 9, 10 and 11. Field 0 is nothing but the leading padding and
# field 1 is "3"; no sensor is mapped to either, which is why the padding on this
# response has never been visible.
MONITER_CGI = response(
    f";3;{DATALOGGER_SERIAL};10010125;Disabled;null;null;Enabled;Airthings;24;"
    "192.168.71.135;92:78:48:DA:27:75;Connected;null;"
)


class Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


@pytest.fixture
def http_app(make_app, monkeypatch):
    """An App polling a stubbed datalogger over HTTP, recording what it publishes."""

    def _make(inverter=INVERTER_CGI, moniter=MONITER_CGI):
        app = make_app()
        app.config["datalogger"]["http"] = {
            "enabled": True,
            "user": "admin",
            "password": "123456789",
        }

        bodies = {"inverter.cgi": inverter, "moniter.cgi": moniter}
        monkeypatch.setattr(
            app_module.requests,
            "get",
            lambda url, **kwargs: Response(bodies[url.rsplit("/", 1)[-1]]),
        )

        return app

    return _make


def value_of(app, name):
    return {topic: payload for topic, payload in app.published}.get(
        f"tcpsolis2mqtt/{name}"
    )


def test_the_captures_are_the_shape_the_datalogger_sends(http_app):
    # The trace is only worth keeping if it stays the response that was captured.
    for body in (INVERTER_CGI, MONITER_CGI):
        assert len(body) == RESPONSE_LENGTH
        assert len(body) - len(body.lstrip("\x00")) == LEADING_PADDING
        assert body.endswith("\x00")

    # The symptom, before anything strips it.
    assert len(INVERTER_CGI.split(";")[0]) == 49


def test_the_inverter_serial_is_published_without_its_padding(http_app):
    app = http_app()
    app.query_http()

    assert value_of(app, "inverter_serial") == INVERTER_SERIAL
    assert len(value_of(app, "inverter_serial")) == 16


def test_no_published_value_holds_a_nul(http_app):
    app = http_app()
    app.query_http()

    for topic, payload in app.published:
        assert "\x00" not in payload, topic


def test_the_fields_that_were_already_clean_are_untouched(http_app):
    # Stripping must not eat real content, so the siblings that always decoded
    # correctly are pinned to the byte.
    app = http_app()
    app.query_http()

    assert value_of(app, "datalogger_serial") == DATALOGGER_SERIAL
    assert value_of(app, "inverter_firmware") == "83003A"
    assert value_of(app, "inverter_model") == "509"
    assert value_of(app, "datalogger_firmware") == "10010125"
    assert value_of(app, "wifi_ssid") == "Airthings"
    assert value_of(app, "wifi_signal") == "24"
    assert value_of(app, "wifi_ip") == "192.168.71.135"
    assert value_of(app, "wifi_mac") == "92:78:48:DA:27:75"


def test_the_fields_nothing_is_mapped_to_decode_to_nothing(make_app):
    # The class of defect, rather than the one instance of it. moniter.cgi field 0 is
    # the leading padding and the last field of either response is the trailing
    # padding, so a sensor mapped to any of them would publish NULs exactly as
    # inverter_serial did. None of them is read today, which is why the list of clean
    # topics looked reassuring.
    app = make_app()

    moniter = MONITER_CGI.split(";")
    inverter = INVERTER_CGI.split(";")

    assert app.http_value(moniter, "moniter", 0) is None
    assert app.http_value(moniter, "moniter", len(moniter) - 1) is None
    assert app.http_value(inverter, "inverter", len(inverter) - 1) is None


def test_a_field_that_is_still_binary_is_refused(http_app):
    # Padding at the ends is expected. A control character in the middle means the
    # field is not the string sensors.yaml describes, and publishing it would poison
    # the recorder exactly as the padding did.
    app = http_app(inverter=response("18050902\x0032050086;83003A;"))
    app.query_http()

    assert value_of(app, "inverter_serial") is None
    assert value_of(app, "inverter_firmware") == "83003A"


def test_a_field_the_response_does_not_have_is_skipped(http_app):
    # sensors.yaml is the only description of these responses, so a shorter one is a
    # missing reading rather than an IndexError out of query_http, query_modbus and
    # main.
    app = http_app(moniter=response(f";3;{DATALOGGER_SERIAL};10010125;"))
    app.query_http()

    assert value_of(app, "datalogger_serial") == DATALOGGER_SERIAL
    assert value_of(app, "wifi_mac") is None
    assert value_of(app, "inverter_serial") == INVERTER_SERIAL


def test_nothing_is_published_when_http_is_disabled(make_app):
    app = make_app()
    app.query_http()

    assert app.published == []
