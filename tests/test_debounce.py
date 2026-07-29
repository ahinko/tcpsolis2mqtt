"""A finished period's total is a step function, so it is guarded by repetition
rather than by plausibility.

On 2026-07-28 generation_yesterday (register 3015) read 81 for almost the whole day
with four one sample excursions to the correct 103.2, at roughly 04:40, 09:00, 22:30
and 23:50. The baseline was the wrong value and the spikes were right, which is
inverted from generation_today. On 2026-07-29 it read 98.8 all morning, correctly,
so it does not happen every day.

A plausibility check is the wrong tool: the value legitimately jumps by a whole
day's generation once a day. One sample is not a rollover, though. A real one
persists.
"""

import pytest

from app import DEBOUNCE_POLLS


@pytest.fixture
def yesterday(sensors_config):
    return next(s for s in sensors_config if s["name"] == "generation_yesterday")


@pytest.fixture
def poll(make_app, yesterday):
    """Feed readings to the debounce, returning which of them would be published."""

    def _poll(*values):
        app = make_app()
        return [app.value_is_settled(yesterday, value) for value in values]

    return _poll


def test_a_new_value_has_to_repeat_before_it_is_published(poll):
    assert poll(*[98.8] * DEBOUNCE_POLLS) == [False] * (DEBOUNCE_POLLS - 1) + [True]


def test_a_settled_value_keeps_publishing(poll):
    published = poll(*[98.8] * (DEBOUNCE_POLLS + 3))

    assert published[-3:] == [True, True, True]


def test_the_observed_excursions_are_held_back(poll):
    # The 2026-07-28 shape: a stable baseline with single sample spikes in it.
    published = poll(81.0, 81.0, 81.0, 103.2, 81.0, 81.0, 103.2, 81.0)

    assert published == [False, False, True, False, True, True, False, True]


def test_a_real_rollover_still_gets_through(poll):
    # Held for a poll or two, then published, which for a figure that only changes
    # once a day is not a delay worth caring about.
    published = poll(81.0, 81.0, 81.0, *[103.2] * DEBOUNCE_POLLS)

    assert published[-1] is True
    assert published[-DEBOUNCE_POLLS:-1] == [False] * (DEBOUNCE_POLLS - 1)


def test_a_flapping_pair_publishes_neither(poll):
    published = poll(81.0, 103.2, 81.0, 103.2, 81.0, 103.2)

    assert published == [False] * 6


def test_the_counters_are_not_debounced(make_app, generation_today, clock):
    # generation_today changes on almost every poll, so requiring repetition would
    # stop it dead. It has the plausibility check instead.
    app = make_app()

    for value in [10.0, 10.1, 10.2]:
        clock.advance(30)
        assert app.value_is_publishable(generation_today, value)


def test_a_reading_that_is_neither_is_published_as_it_arrives(make_app, sensors_config):
    app = make_app()
    voltage = next(s for s in sensors_config if s["name"] == "ac_a_phase_voltage")

    for value in [230.1, 231.4, 229.8]:
        assert app.value_is_publishable(voltage, value)


def test_the_finished_totals_are_the_debounced_ones(make_app, sensors_config):
    app = make_app()
    debounced = {
        sensor["name"]
        for sensor in sensors_config
        if app.is_finished_period_total(sensor)
    }

    assert debounced == {
        "generation_yesterday",
        "generation_last_month",
        "generation_last_year",
    }
