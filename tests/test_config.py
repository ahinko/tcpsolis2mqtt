"""max_power_kw is the nameplate rating of a specific inverter, and the plausibility
check measures every energy counter reading against it. A default is worse than no
value: too high and a stale reading walks straight through, too low and real
generation is refused. So it is required, and so is the block holding it."""

import pytest
from config import AppConfig
from marshmallow import ValidationError


def config(**inverter):
    return {
        "datalogger": {"host": "192.0.2.1"},
        "inverter": {"name": "Solis", "max_power_kw": 15} | inverter,
        "mqtt": {"enabled": True, "host": "192.0.2.2"},
    }


def test_a_complete_config_loads():
    assert AppConfig().load(config())["inverter"]["max_power_kw"] == 15


def test_max_power_kw_is_required():
    incomplete = config()
    del incomplete["inverter"]["max_power_kw"]

    with pytest.raises(ValidationError, match="max_power_kw"):
        AppConfig().load(incomplete)


def test_the_inverter_block_is_required():
    # Otherwise max_power_kw is dodged by leaving the whole block out, and the app
    # fails with a KeyError at the first reading instead of a message at startup.
    incomplete = config()
    del incomplete["inverter"]

    with pytest.raises(ValidationError, match="inverter"):
        AppConfig().load(incomplete)


def test_the_rest_of_the_inverter_block_stays_optional():
    minimal = {
        "datalogger": {"host": "192.0.2.1"},
        "inverter": {"max_power_kw": 3.6},
        "mqtt": {"enabled": True, "host": "192.0.2.2"},
    }

    assert AppConfig().load(minimal)["inverter"]["model"] == ""


def test_a_fractional_rating_is_kept():
    # Plenty of domestic inverters are not whole kW.
    assert AppConfig().load(config(max_power_kw=3.68))["inverter"]["max_power_kw"] == (
        3.68
    )


def test_the_shipped_example_names_a_rating():
    import yaml

    example = yaml.safe_load(open("config.example.yaml"))

    assert example["inverter"]["max_power_kw"]
