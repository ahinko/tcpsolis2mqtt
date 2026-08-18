"""What happens when the datalogger accepts a CGI connection and then says nothing.

requests has no default timeout, so before HTTP_TIMEOUT existed this hung the poll
loop for good. Nothing reported it: paho's network thread keeps the broker session
alive, so the Last Will never fires and Home Assistant sees an app that is up and has
quietly stopped publishing, every sensor frozen on its last value.

query_http only runs on the transition back to online, so the hang would happen at
the moment the datalogger is coming back from being flaky.
"""

import pytest

import app as app_module


class Response:
    def __init__(self, text=";;;;;;;;;;;;;;", status_code=200):
        self.text = text
        self.status_code = status_code


@pytest.fixture
def http_app(make_app, monkeypatch):
    """An App whose CGI requests are recorded, or made to fail on demand."""

    def _make(raises=None):
        app = make_app()
        app.config["datalogger"]["http"] = {
            "enabled": True,
            "user": "admin",
            "password": "123456789",
        }

        app.requests_made = []

        def get(url, **kwargs):
            app.requests_made.append((url, kwargs))
            if raises is not None:
                raise raises
            return Response()

        monkeypatch.setattr(app_module.requests, "get", get)

        return app

    return _make


def test_every_cgi_request_carries_a_timeout(http_app):
    app = http_app()
    app.query_http()

    assert len(app.requests_made) == 2, "inverter.cgi and moniter.cgi"

    for url, kwargs in app.requests_made:
        assert kwargs.get("timeout") == app_module.HTTP_TIMEOUT, url


def test_the_timeout_is_a_connect_and_a_read_timeout(http_app):
    # A single number would only bound the connect. The datalogger's failure is to
    # accept the connection and then never send the body, which is the read half.
    connect, read = app_module.HTTP_TIMEOUT

    assert connect > 0 and read > 0


def test_a_timeout_is_survived(http_app):
    app = http_app(raises=app_module.requests.exceptions.Timeout("timed out"))

    app.query_http()

    assert app.published == [], "nothing to publish, and nothing raised"


def test_a_timeout_does_not_stop_the_next_poll_trying_again(http_app):
    app = http_app(raises=app_module.requests.exceptions.Timeout("timed out"))

    app.query_http()
    app.query_http()

    assert len(app.requests_made) == 2, "one attempt per call, no state left behind"
