"""The datalogger intermittently reports a value belonging to a previous day.

Every scenario here is taken from what the inverter actually did, or from a case it
is expected to survive. The numbers matter: 103.2 kWh was the real generation on
2026-07-27, and the readings around sunrise on 2026-07-28 arrived 30 seconds apart
after a six hour gap with no successful poll.
"""

import arrow
import pytest


@pytest.fixture
def poll(clock, generation_today):
    """Feed one reading to the plausibility gate, `after` seconds since the last."""

    def _poll(app, value, after=30):
        clock.advance(after)
        return app.value_is_plausible(generation_today, value)

    return _poll


def new_day(app, clock, previous_total, day="2026-07-29"):
    """Run the midnight reset, having produced `previous_total` the day before."""
    app.current_day = str(arrow.get(day).shift(days=-1).date())
    app.last_accepted_value["generation_today"] = (previous_total, clock.now)
    app.day = day
    clock.advance(3600)
    app.reset_counters()


def test_stale_total_rejected_when_it_arrives_first(make_app, clock, poll):
    # A cloudy day only yields 40 kWh, which is small enough that six hours of
    # accumulated rate limit allowance would otherwise let it through.
    app = make_app()
    new_day(app, clock, previous_total=40.0)
    clock.advance(6 * 3600)

    assert not poll(app, 40.0), "yesterday's total is not today's generation"
    assert poll(app, 0.0), "the register really being reset must be accepted"
    assert not poll(app, 40.0), "the flicker back to the stale value must be rejected"
    assert poll(app, 0.1), "real production must get through"


def test_observed_sunrise_trace(make_app, clock, poll):
    # 2026-07-28: the counter read 0, flicked back to yesterday's 103.2 while the
    # inverter dropped out, then settled at 0 once it stayed up.
    app = make_app()
    new_day(app, clock, previous_total=103.2)
    clock.advance(6 * 3600)

    assert poll(app, 0.0)
    assert not poll(app, 103.2), "the 0 -> 103.2 rise Home Assistant counted as real"
    assert not poll(app, 103.2)
    assert poll(app, 0.0)


def test_guard_disarms_so_today_can_reach_yesterdays_total(make_app, clock, poll):
    app = make_app()
    new_day(app, clock, previous_total=40.0)
    clock.advance(6 * 3600)
    poll(app, 0.0)

    # A real day's climb, an hour between readings, up to yesterday's figure.
    for value in [10.0, 20.0, 30.0, 39.0]:
        assert poll(app, value, after=3600)

    for value in [39.8, 39.9, 40.0, 40.1, 40.2]:
        assert poll(app, value, after=600), f"{value} must publish once disarmed"


def test_winter_day(make_app, clock, poll):
    # Four kWh over a whole short day, after a long night.
    app = make_app()
    new_day(app, clock, previous_total=4.0, day="2026-12-15")
    clock.advance(9 * 3600)

    assert not poll(app, 4.0), "yesterday's 4 kWh is not today's"
    assert poll(app, 0.0)

    for value in [0.1, 0.5, 1.2, 2.8, 3.9, 4.0, 4.1]:
        assert poll(app, value, after=900), f"{value} must publish on a winter day"


def test_day_with_no_production_at_all(make_app, clock, poll):
    app = make_app()
    new_day(app, clock, previous_total=0.0, day="2026-12-21")
    clock.advance(9 * 3600)

    # Indistinguishable from yesterday's total, but the reset already published 0
    # so Home Assistant is showing the right thing anyway.
    assert not poll(app, 0.0)
    assert poll(app, 0.3, after=900), "production must still get through"
    assert poll(app, 0.9, after=900)


def test_genuine_value_after_a_restart_across_midnight(make_app, clock, poll):
    # The container was down all night and only starts at 10:00, by which time the
    # inverter has legitimately produced 25 kWh.
    app = make_app()
    new_day(app, clock, previous_total=40.0)
    clock.advance(10 * 3600)

    assert poll(app, 25.0), "25 kWh must not be mistaken for yesterday's 40"


def test_normal_production_is_untouched(make_app, clock, poll):
    app = make_app()

    for value in [10.0, 10.1, 10.2, 10.4, 10.6, 10.8]:
        assert poll(app, value)


def test_stale_value_landing_mid_day(make_app, clock, poll):
    app = make_app()
    poll(app, 10.8)

    assert not poll(app, 103.2), "a jump of 92 kWh in 30 seconds is impossible"
    assert poll(app, 11.0)


def test_decrease_is_always_allowed(make_app, clock, poll):
    # A genuine counter reset looks exactly like this.
    app = make_app()
    poll(app, 50.0)

    assert poll(app, 0.0)


def test_catch_up_after_an_outage(make_app, clock, poll):
    app = make_app()
    poll(app, 10.0)

    assert poll(app, 75.0, after=6 * 3600), "a long gap allows a large real increase"


def test_whole_kwh_counter_can_tick_over(make_app, clock, energy_this_month):
    # A counter reporting whole kWh moves in steps of 1, which is larger than the
    # 0.15 kWh a 15 kW inverter can produce between two 30 second polls.
    app = make_app()

    for value in [500, 501, 502]:
        clock.advance(30)
        assert app.value_is_plausible(energy_this_month, value)


def test_first_reading_of_a_run_is_adopted(make_app, clock, poll):
    # Nothing to compare against yet, so it becomes the baseline.
    app = make_app()

    assert poll(app, 62.5)


def test_a_lifetime_counter_may_not_decrease(make_app, clock, total_power):
    # total_power has no resets:, so a drop is a fault rather than a reading. It used
    # to be caught by flagging the register never_zero; now it follows from the
    # register never being cleared. Accepting the 0 would leave the sensor stuck for
    # about three months, since climbing back to 39901 needs that much allowance.
    app = make_app()
    clock.advance(30)
    assert app.value_is_plausible(total_power, 39901)

    clock.advance(30)
    assert not app.value_is_plausible(total_power, 0)

    clock.advance(30)
    assert app.value_is_plausible(total_power, 39902), "the real reading still lands"


def test_a_lifetime_counter_restored_after_a_restart_holds_its_floor(
    make_app, clock, total_power
):
    # load_state seeds the baseline from the retained topic with no timestamp, because
    # how long the container was down is unknown.
    app = make_app()
    app.last_accepted_value["total_power"] = (39901, None)

    clock.advance(30)
    assert not app.value_is_plausible(total_power, 0)

    clock.advance(30)
    assert app.value_is_plausible(total_power, 40100), (
        "a jump that a long outage explains must not be measured against an "
        "allowance that does not exist"
    )

    clock.advance(30)
    assert not app.value_is_plausible(total_power, 41000), "and guarded from then on"
