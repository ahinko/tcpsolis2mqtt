"""How long the connection to the datalogger lives, and what the socket is told.

For modbus TCP, dialling a fresh connection for every read is the unusual choice; one
connection held open is the normal one. This datalogger makes that a trade rather than
an improvement: it accepts a single connection at a time, so holding ours means
nothing else -- Solis Cloud included -- can reach the stick, where hanging up leaves a
gap in every poll cycle where something else can. Hence the setting, and hence off by
default.

The setting is expected to be temporary. These sticks are widely reported to hang up
on an idle connection after a minute or two, and at a 30 second poll interval that
would mean reconnecting nearly every poll and gaining nothing. That cannot be settled
from here, only measured, which is what the one log line per connection is for: turn
it on, leave it a day, count them.
"""

import socket

from app import KEEPALIVE_IDLE, KEEPALIVE_INTERVAL, KEEPALIVE_PROBES
from conftest import SPAN, StubClient, live_response

KEPT = {"persistent_connection": True}


def connection_lines(caplog):
    return [
        record.message
        for record in caplog.records
        if record.message.startswith("Connected to datalogger")
    ]


def test_the_connection_is_dropped_after_every_poll_by_default(polls):
    app = polls((live_response(),), (live_response(),))

    assert app.stub_client.connects == 2
    assert app.stub_client.closes == 2
    assert app.client is None


def test_the_connection_is_kept_between_polls_when_it_is_asked_for(polls):
    app = polls((live_response(),), (live_response(),), (live_response(),), **KEPT)

    assert app.stub_client.connects == 1, "dialled once, then reused"
    assert app.stub_client.closes == 0
    assert app.client is app.stub_client


def test_a_kept_connection_still_reads_the_whole_span_every_poll(polls):
    app = polls((live_response(),), (live_response(),), **KEPT)

    assert app.stub_client.reads == [(3004, SPAN), (3004, SPAN)]


def test_a_poll_that_could_not_read_the_span_lets_the_connection_go(polls):
    # A connection the datalogger would not answer over is not one to hold on to, and
    # holding it would keep everything else off the stick for nothing. This is also
    # what every poll did before the connection could be kept, so turning the setting
    # on cannot make a bad morning worse than it already was.
    app = polls((), **KEPT)

    assert app.client is None
    assert app.stub_client.closes == 1


def test_the_poll_after_a_failure_dials_a_fresh_connection(polls):
    app = polls((live_response(),), (), (live_response(),), **KEPT)

    assert app.stub_client.connects == 2
    assert app.client is app.stub_client


def test_a_dead_socket_is_redialled_inside_the_poll(query):
    # The raised error drops the connection, and the attempt after it dials again
    # rather than writing into the same dead socket. With the connection kept, that
    # redial is the app's own rather than something pymodbus does out of sight.
    app, client, registers = query(
        OSError("Connection reset by peer"), live_response(), **KEPT
    )

    assert client.connects == 2
    assert len(registers) == SPAN


def test_a_reused_connection_is_not_evidence_the_datalogger_is_there(polls):
    # There is no handshake to be fooled by when the connection is reused, so the
    # first chunk of registers that arrives is the only thing that can say "online".
    # A poll whose reads are all refused has to count against the retry budget the
    # same as any other, or the datalogger is never declared offline at all.
    app = polls((live_response(),), (), **KEPT)

    assert app.retries_done == 1


def hangs_up_after_every_poll():
    # The failure mode this setting is most likely to meet. These sticks are reported
    # to drop an idle connection, and pymodbus answers that by closing its own socket
    # and dialling a new one on the next request -- silently, so the reconnects the
    # setting exists to count would never appear, and the new socket would carry none
    # of the keepalive this app sets.
    return StubClient(hangs_up_after_read=True)


def test_a_datalogger_that_hung_up_is_redialled_rather_than_reused(polls):
    app = polls(
        (live_response(),),
        (live_response(),),
        client=hangs_up_after_every_poll(),
        **KEPT,
    )

    assert app.stub_client.connects == 2
    assert app.stub_client.sockets[1].options == app.keepalive_options()


def test_a_connection_pymodbus_had_already_closed_says_so(polls, caplog):
    caplog.set_level("INFO")

    polls(
        (live_response(),),
        (live_response(),),
        client=hangs_up_after_every_poll(),
        **KEPT,
    )

    assert "pymodbus had already closed the socket" in connection_lines(caplog)[1]


def test_every_connection_is_logged_with_why_the_last_one_ended(polls, caplog):
    caplog.set_level("INFO")

    polls((live_response(),), (), (live_response(),), **KEPT)

    first, second = connection_lines(caplog)

    assert "connection 1 since startup" in first
    assert "nothing has been connected yet" in first
    assert "connection 2 since startup" in second
    assert "the register span could not be read in full" in second


def test_a_lost_socket_says_so_in_the_line_for_the_connection_after_it(query, caplog):
    caplog.set_level("INFO")

    query(OSError("Connection reset by peer"), live_response(), **KEPT)

    assert (
        "a read raised OSError: Connection reset by peer" in connection_lines(caplog)[1]
    )


def test_a_connection_per_poll_is_not_worth_a_line_a_poll(polls, caplog):
    # The default mode dials every poll by design, so at a 30 second interval this
    # would be 2880 lines a day saying only that the app is working as configured.
    caplog.set_level("INFO")

    polls((live_response(),), (live_response(),))

    assert connection_lines(caplog) == []


def test_the_connections_are_still_there_at_debug(polls, caplog):
    caplog.set_level("DEBUG")

    polls((live_response(),), (live_response(),))

    assert len(connection_lines(caplog)) == 2


def test_keepalive_is_switched_on(make_app):
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in make_app().keepalive_options()


def test_the_kernel_is_told_how_long_to_wait_and_how_often_to_probe(make_app):
    # Without these, a datalogger that vanished while we slept is only discovered by
    # the next read timing out, three times over -- most of a poll interval spent
    # finding out something the kernel could have established during the sleep.
    values = [value for _, _, value in make_app().keepalive_options()]

    assert values == [1, KEEPALIVE_IDLE, KEEPALIVE_INTERVAL, KEEPALIVE_PROBES]


def test_every_declared_option_reaches_the_socket(query):
    app, client, _ = query(live_response())

    assert client.sockets[0].options == app.keepalive_options()


def test_a_redialled_connection_gets_its_own_keepalive(query):
    # A new socket starts with the system defaults, which on Linux is a first probe
    # after two hours.
    app, client, registers = query(OSError("Connection reset by peer"), live_response())

    assert client.sockets[1].options == app.keepalive_options()


def test_a_socket_that_refuses_the_options_still_gets_polled(query, caplog):
    # Keepalive makes a vanished datalogger surface sooner. Without it the read
    # timeouts still find out, just slower, which is not worth failing a poll over.
    caplog.set_level("WARNING")
    client = StubClient(live_response(), refuse_socket_options=True)

    app, client, registers = query(client=client)

    assert len(registers) == SPAN
    assert "Could not set TCP keepalive" in caplog.records[0].message
