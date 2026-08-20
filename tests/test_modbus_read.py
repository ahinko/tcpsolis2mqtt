"""How a chunk of registers is read, and what happens when the datalogger will not
give one up.

The numbers come from the morning of 2026-07-29, when the datalogger came up and
fell over five times between 05:30 and 05:43. Single poll cycles made 73, 433,
447, 405 and 473 read attempts, because a chunk that raised was retried without
advancing and without sleeping. 1828 failed reads over thirteen hours, and five
complete 80 register responses discarded on top of that, because the expected
register count was incremented once per attempt rather than once per chunk.
"""

import app as app_module

# The stub datalogger, the register span and the query/polls fixtures live in
# conftest.py, because the connection lifetime tests in test_persistent_connection.py
# poll the same stub.
from conftest import FIRST, LAST, SPAN, Response, StubClient, live_response


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


def test_a_broken_pipe_is_recovered_from_instead_of_killing_the_process(query):
    # This used to be os._exit(1): kill the container and start again, losing the
    # plausibility timestamps and the debounce counts with it. The path could not even
    # be tested, because the exit would have taken the test runner down too.
    app, client, registers = query(OSError("[Errno 32] Broken pipe"), live_response())

    assert len(registers) == SPAN
    assert len(client.reads) == 2, "retried, and the retry worked"


def test_a_raised_error_drops_the_socket_so_the_next_attempt_redials(query):
    # pymodbus connect() returns true whenever it still holds a socket object,
    # without testing whether anything answers on it. Without the close, every later
    # request goes into the same dead socket.
    #
    # A reset rather than a broken pipe, so this case can be run against the old
    # behaviour. A broken pipe there called os._exit and would take the test runner
    # with it, which is the other half of why that path was never covered.
    app, client, registers = query(OSError("Connection reset by peer"), live_response())

    assert client.closes == 2, "once for the dead socket, once at the end of the poll"


def test_a_refused_read_does_not_drop_the_socket(query):
    # An isError response is the datalogger declining to answer, which is what it
    # does every morning while the inverter wakes up. The connection is fine, and
    # reconnecting on each of those would be churn for nothing.
    app, client, registers = query(Response(error=True), live_response())

    assert client.closes == 1, "only the close at the end of the poll"


def test_only_the_registers_that_are_wanted_are_asked_for(query):
    # Whole chunks were read regardless of the span, so the default chunk size asked
    # for six registers past the end of anything sensors.yaml maps.
    app, client, registers = query(live_response())

    assert client.reads == [(FIRST, SPAN)]
    assert max(registers) == LAST


def test_a_chunk_size_that_lands_on_the_end_still_reads_the_last_register(
    make_app, clock, monkeypatch
):
    # The bug the range bound hid. With the span 3004 to 3077, a register_chunks of 73
    # read 3004 to 3076 and stopped: the length check passed, because 73 were asked
    # for and 73 arrived, so the poll was kept and only the sensor needing 3077
    # failed, as a KeyError logged once a poll forever.
    app = make_app(register_chunks=73)
    app.get_register_interval()

    client = StubClient(live_response(count=73), live_response(address=3077, count=1))
    monkeypatch.setattr(app_module, "ModbusTcpClient", lambda *a, **kw: client)

    registers = app.query_modbus()

    assert client.reads == [(FIRST, 73), (LAST, 1)]
    assert LAST in registers
    assert len(registers) == SPAN
