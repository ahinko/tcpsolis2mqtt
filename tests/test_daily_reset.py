"""The inverter is asleep at midnight and only clears its own daily register hours
later, so the reset is published on the wall clock instead."""

VALUE_TOPIC = "tcpsolis2mqtt/generation_today"
DAY_TOPIC = "tcpsolis2mqtt/_state/current_day"
PREVIOUS_TOPIC = "tcpsolis2mqtt/_state/generation_today/previous_total"


def test_nothing_happens_while_the_day_is_unchanged(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"

    app.reset_daily_counters()

    assert app.published == []


def test_new_day_publishes_zero(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.last_accepted_value["generation_today"] = (103.2, clock.now)
    app.day = "2026-07-29"

    app.reset_daily_counters()

    assert (VALUE_TOPIC, 0) in app.published
    assert (DAY_TOPIC, "2026-07-29") in app.published
    assert app.current_day == "2026-07-29"


def test_new_day_remembers_yesterdays_total(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.last_accepted_value["generation_today"] = (103.2, clock.now)
    app.day = "2026-07-29"

    app.reset_daily_counters()

    assert app.previous_day_total["generation_today"] == 103.2
    assert app.awaiting_new_day["generation_today"] is True
    assert (PREVIOUS_TOPIC, 103.2) in app.published


def test_new_day_moves_the_rate_limit_baseline(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.last_accepted_value["generation_today"] = (103.2, clock.now)
    app.day = "2026-07-29"

    app.reset_daily_counters()

    assert app.last_accepted_value["generation_today"][0] == 0


def test_a_second_pass_on_the_same_day_does_nothing(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.day = "2026-07-29"

    app.reset_daily_counters()
    published_after_reset = len(app.published)
    app.reset_daily_counters()

    assert len(app.published) == published_after_reset


def test_only_sensors_flagged_resets_daily_are_touched(make_app, clock):
    app = make_app()
    app.current_day = "2026-07-28"
    app.day = "2026-07-29"

    app.reset_daily_counters()

    reset_topics = {topic for topic, _ in app.published if not topic.count("_state")}
    assert reset_topics == {VALUE_TOPIC}
