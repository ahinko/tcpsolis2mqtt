"""How a chunk of registers is read, and what happens when the datalogger will not
give one up.

The numbers come from the morning of 2026-07-29, when the datalogger came up and
fell over five times between 05:30 and 05:43. Single poll cycles made 73, 433,
447, 405 and 473 read attempts, because a chunk that raised was retried without
advancing and without sleeping. 1828 failed reads over thirteen hours, and five
complete 80 register responses discarded on top of that, because the expected
register count was incremented once per attempt rather than once per chunk.
"""

import pytest

import app as app_module

SPAN = 80


class Response:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self.error = error

    def isError(self):
        return self.error


class StubClient:
    """A datalogger that answers with whatever the test queued up."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.reads = []
        self.connected = True

    def connect(self):
        return True

    def close(self):
        self.connected = False

    def read_input_registers(self, device_id, address, count):
        self.reads.append((address, count))
        answer = self.answers.pop(0) if self.answers else Response(error=True)

        if isinstance(answer, Exception):
            raise answer

        return answer


def live_response(address=3004, count=SPAN):
    # Register 3041 is the inverter temperature, the only thing that has to be non
    # zero for the response not to count as dead.
    return Response([250 if address + i == 3041 else 0 for i in range(count)])


@pytest.fixture
def query(make_app, clock, monkeypatch):
    """Run one poll against a stub datalogger and return what it produced."""

    def _query(*answers):
        client = StubClient(*answers)
        app = make_app()
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

    def _polls(*rounds):
        app = make_app()
        app.get_register_interval()
        client = StubClient()

        def build(*args, **kwargs):
            return client

        monkeypatch.setattr(app_module, "ModbusTcpClient", build)

        for answers in rounds:
            client.answers = list(answers)
            app.query_modbus()

        return app

    return _polls


def test_a_chunk_is_read_once_when_it_answers(query):
    app, client, registers = query(live_response())

    assert len(client.reads) == 1
    assert len(registers) == SPAN


def test_a_failing_chunk_is_not_retried_forever(query):
    app, client, registers = query()

    assert len(client.reads) == app_module.CHUNK_ATTEMPTS
    assert registers == {}


def test_a_raising_chunk_is_not_retried_forever(query):
    app, client, registers = query(*[OSError("Connection reset by peer")] * 5)

    assert len(client.reads) == app_module.CHUNK_ATTEMPTS
    assert registers == {}


def test_retries_are_spaced_out(query, clock):
    query(*[OSError("boom")] * 5)

    # One pause between attempts, none after the last, so the loop cannot spin.
    assert clock.now == (app_module.CHUNK_ATTEMPTS - 1) * app_module.CHUNK_RETRY_DELAY


def test_a_complete_response_after_a_retry_is_kept(query):
    # The bug this pins: the expected register count grew by a whole chunk on every
    # attempt, so a response that finally arrived was compared against an inflated
    # total and thrown away.
    app, client, registers = query(Response(error=True), live_response())

    assert len(client.reads) == 2
    assert len(registers) == SPAN


def test_a_chunk_that_never_answers_takes_the_datalogger_offline(query):
    app, client, registers = query()

    assert app.datalogger_unreachable
    assert app.retries_done == 1, "one failed poll cycle, not one per read attempt"


def test_registers_are_numbered_from_the_address_they_were_read_at(query):
    app, client, registers = query(live_response())

    assert min(registers) == 3004
    assert registers[3041] == 250


def test_an_all_zero_response_is_discarded(query):
    app, client, registers = query(Response([0] * SPAN))

    assert registers == {}


def test_the_client_is_not_allowed_its_own_retry_loop(query):
    # pymodbus runs `while count_retries <= retries`, so its default of 3 is four
    # attempts per request. Multiplied by CHUNK_ATTEMPTS that is twelve requests for
    # one chunk, each waiting the full timeout, and neither constant in this file
    # says so. read_chunk does the retrying; the client does exactly one attempt.
    app, _, _ = query()

    assert app.client_kwargs["retries"] == 0


def test_the_client_waits_the_timeout_this_module_declares(query):
    app, _, _ = query()

    assert app.client_kwargs["timeout"] == app_module.MODBUS_TIMEOUT


def test_the_client_is_not_given_a_setting_that_does_nothing(query):
    # reconnect_delay was passed for years. The pymodbus docs are explicit that it is
    # not used by the sync client, and the client was rebuilt every poll anyway, so
    # it only ever read as though reconnection were handled somewhere.
    app, _, _ = query()

    assert "reconnect_delay" not in app.client_kwargs


def test_a_connection_that_opens_is_not_proof_the_datalogger_is_there(query):
    # The morning of 2026-08-19: the datalogger accepted the connection and then
    # refused every register while the inverter woke up. Announcing online on the
    # strength of the handshake reset the retry counter on each of those polls.
    app, client, registers = query()

    assert registers == {}
    assert app.retries_done == 1, "the failed poll counted"


def test_failed_polls_accumulate_towards_offline(polls):
    # The bug this pins. Thirteen consecutive failures in the real incident, all of
    # them logged "done 0 of 20 retries", because each poll's successful connect
    # cleared the count the poll before had added.
    app = polls((), (), ())

    assert app.retries_done == 3


def test_failed_polls_eventually_declare_the_datalogger_offline(polls, make_app):
    retries = make_app().config["datalogger"]["poll_retries"]

    app = polls(*[()] * (retries + 1))

    assert app.datalogger_offline
    assert dict(app.published)["tcpsolis2mqtt/datalogger_availability"] == "offline"


def test_a_poll_that_reads_registers_says_the_datalogger_is_online(polls):
    app = polls((live_response(),))

    assert not app.datalogger_offline
    assert app.retries_done == 0
    assert dict(app.published)["tcpsolis2mqtt/datalogger_availability"] == "online"


def test_reads_that_start_working_again_clear_the_count(polls):
    app = polls((), (), (live_response(),))

    assert app.retries_done == 0
    assert not app.datalogger_offline
