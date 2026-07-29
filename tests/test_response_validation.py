"""The datalogger sometimes answers with a complete, well formed block of registers
where every value is zero, usually while the inverter is asleep. A live inverter
cannot produce that: AC voltage alone reads about 2300."""

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


def test_a_single_live_register_saves_the_response(make_app):
    # Deliberately weak: the length check in query_modbus is what catches a partial
    # response, this only has to condemn the all zero one.
    assert not make_app().response_is_dead({3040: 0, 3041: 250, 3042: 0})
