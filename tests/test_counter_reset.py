"""The inverter is asleep at midnight and only clears its own daily register hours
later, so the reset is published on the wall clock instead. The same holds at a
month and a year boundary, where the register that has to be cleared is a
different one."""

TODAY = "tcpsolis2mqtt/generation_today"
THIS_MONTH = "tcpsolis2mqtt/energy_this_month"
THIS_YEAR = "tcpsolis2mqtt/generation_this_year"
DAY_TOPIC = "tcpsolis2mqtt/_state/current_day"
PREVIOUS_TOPIC = "tcpsolis2mqtt/_state/generation_today/previous_total"


def reset_topics(app):
    return {topic for topic, _ in app.published if "_state" not in topic}


def test_nothing_happens_while_the_day_is_unchanged(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"

    app.reset_counters()

    assert app.published == []


def test_new_day_publishes_zero(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.last_accepted_value["generation_today"] = (103.2, clock.now)
    app.day = "2026-07-29"

    app.reset_counters()

    assert (TODAY, 0) in app.published
    assert (DAY_TOPIC, "2026-07-29") in app.published
    assert app.current_day == "2026-07-29"


def test_new_day_remembers_yesterdays_total(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.last_accepted_value["generation_today"] = (103.2, clock.now)
    app.day = "2026-07-29"

    app.reset_counters()

    assert app.previous_period_total["generation_today"] == 103.2
    assert app.awaiting_new_period["generation_today"] is True
    assert (PREVIOUS_TOPIC, 103.2) in app.published


def test_new_day_moves_the_plausibility_baseline(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.last_accepted_value["generation_today"] = (103.2, clock.now)
    app.day = "2026-07-29"

    app.reset_counters()

    assert app.last_accepted_value["generation_today"][0] == 0


def test_a_second_pass_on_the_same_day_does_nothing(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.day = "2026-07-29"

    app.reset_counters()
    published_after_reset = len(app.published)
    app.reset_counters()

    assert len(app.published) == published_after_reset


def test_an_ordinary_day_leaves_the_month_and_year_alone(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.day = "2026-07-29"

    app.reset_counters()

    assert reset_topics(app) == {TODAY}


def test_the_first_of_the_month_resets_the_month_too(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-31"
    app.day = "2026-08-01"

    app.reset_counters()

    assert reset_topics(app) == {TODAY, THIS_MONTH}


def test_new_years_day_resets_all_three(make_app, clock):
    app = make_app()
    app.current_day = "2026-12-31"
    app.day = "2027-01-01"

    app.reset_counters()

    assert reset_topics(app) == {TODAY, THIS_MONTH, THIS_YEAR}


def test_the_month_total_is_remembered_at_the_boundary(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-31"
    app.last_accepted_value["energy_this_month"] = (812, clock.now)
    app.day = "2026-08-01"

    app.reset_counters()

    assert app.previous_period_total["energy_this_month"] == 812
    assert app.awaiting_new_period["energy_this_month"] is True


def test_a_lifetime_counter_is_never_reset(make_app, clock):
    # total_power has no resets:, so nothing may publish a 0 over it.
    app = make_app()
    app.current_day = "2026-12-31"
    app.day = "2027-01-01"

    app.reset_counters()

    assert "tcpsolis2mqtt/total_power" not in reset_topics(app)
