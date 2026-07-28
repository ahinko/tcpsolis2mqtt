"""The datalogger sometimes answers with a complete block of registers where every
value is zero. Sensors flagged never_zero hold lifetime counters, which cannot be
zero on a commissioned inverter, so a zero there condemns the whole response."""

LIVE = {3008: 0, 3009: 21000}


def test_live_response_is_accepted(make_app):
    assert not make_app().response_is_dead(LIVE)


def test_all_zero_response_is_rejected(make_app):
    assert make_app().response_is_dead({number: 0 for number in range(3004, 3084)})


def test_high_word_alone_may_be_zero(make_app):
    # total_power is a 32 bit value, so the high word is zero below 65536 kWh.
    assert not make_app().response_is_dead({3008: 0, 3009: 1})


def test_missing_registers_are_treated_as_zero(make_app):
    assert make_app().response_is_dead({})
