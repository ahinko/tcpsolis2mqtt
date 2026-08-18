"""When the next poll is due.

The interval is meant to be the gap between the starts of two polls. Sleeping the
whole interval after the work instead added the length of the poll to every cycle.
Normally that is a second or two and nobody notices, but a poll that has to retry a
chunk of registers takes far longer than the interval that follows it, so the cycle
that most needs to come round again is the one that was delayed most.
"""

import pytest


@pytest.fixture
def app(make_app, clock):
    return make_app()


def test_a_quick_poll_is_followed_by_the_rest_of_the_interval(app, clock):
    started = 0
    clock.advance(2)

    assert app.seconds_until_next_poll(started) == 28


def test_an_instant_poll_waits_the_whole_interval(app):
    assert app.seconds_until_next_poll(0) == 30


def test_a_poll_that_overran_starts_the_next_one_immediately(app, clock):
    # 40 seconds of retrying against a 30 second interval. Before this the app then
    # slept another 30 on top, making the gap 70.
    started = 0
    clock.advance(40)

    assert app.seconds_until_next_poll(started) == 0


def test_the_wait_is_never_negative(app, clock):
    clock.advance(6000)

    assert app.seconds_until_next_poll(0) == 0


def test_an_offline_datalogger_gets_the_longer_interval(app, clock):
    app.datalogger_offline = True
    clock.advance(10)

    assert app.seconds_until_next_poll(0) == 590


def test_a_slow_poll_leaves_no_debt_for_the_next_one(app, clock):
    # Measured from the start of each poll, not from a running deadline, so one poll
    # that overran cannot make the following ones fire back to back to catch up.
    clock.advance(40)
    assert app.seconds_until_next_poll(0) == 0

    started = clock.now
    clock.advance(1)

    assert app.seconds_until_next_poll(started) == 29
