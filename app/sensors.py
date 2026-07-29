from marshmallow import Schema, fields, validate, validates_schema, ValidationError


class Bit(Schema):
    default_value = fields.Str(required=True)
    map = fields.Dict(required=True)

    @validates_schema()
    def map_length(self, data, **kwargs):
        if len(data["map"]) != 16:
            raise ValidationError("The modbus.bit.map length must be 16")


class Modbus(Schema):
    register = fields.Int(required=True)
    read_type = fields.Str(
        validate=validate.OneOf(
            choices=["register", "long", "bit", "alarm", "composed_datetime"]
        )
    )
    function_code = fields.Int(required=True)
    scale = fields.Float(required=False)
    decimals = fields.Int(required=False)
    # Which period the inverter clears this register on. A statement about the
    # register, not about what the app should do with it: the wall clock reset and
    # the stale total guard are derived from it, and its absence means the register
    # is a lifetime counter that must never decrease.
    resets = fields.Str(
        required=False, validate=validate.OneOf(choices=["daily", "monthly", "yearly"])
    )
    bit = fields.Nested(Bit(), required=False)

    @validates_schema()
    def bit_required_if_read_type_bit(self, data, **kwargs):
        if data["read_type"] == "bit" and "bit" not in data:
            raise ValidationError("Must specify BIT config if read type is bit")


class Http(Schema):
    endpoint = fields.Str(
        required=True, validate=validate.OneOf(choices=["inverter", "moniter"])
    )
    register = fields.Int(required=True)


class HomeAssistant(Schema):
    device = fields.Str(required=True)
    state_class = fields.Str(required=False, allow_none=True, load_default="")
    device_class = fields.Str(required=False, allow_none=True, load_default="")
    payload_on = fields.Str(required=False)
    payload_off = fields.Str(required=False)

    @validates_schema()
    def payload_if_binary_sensor(self, data, **kwargs):
        if (
            data["device"] == "binary_sensor"
            and "payload_on" not in data
            and "payload_off" not in data
        ):
            raise ValidationError(
                "payload_on and payload_off required for binary sensors"
            )


class Sensor(Schema):
    name = fields.Str(required=True)
    description = fields.Str(required=True)
    unit = fields.Str(required=False, allow_none=True, load_default="")
    active = fields.Bool(required=False, load_default=False)
    modbus = fields.Nested(Modbus(), required=False)
    http = fields.Nested(Http(), required=False)
    homeassistant = fields.Nested(HomeAssistant(), required=False)

    @validates_schema()
    def validate_modbus_http(self, data, **kwargs):
        if "modbus" not in data and "http" not in data:
            raise ValidationError("Modbus or Http must be defined")

    @validates_schema()
    def resets_requires_a_counter(self, data, **kwargs):
        # The reset and plausibility logic reads the Home Assistant metadata to
        # decide which registers are cumulative counters. Fail at startup rather
        # than let the two descriptions of the same register drift apart.
        if "modbus" not in data or "resets" not in data["modbus"]:
            return

        homeassistant = data.get("homeassistant", {})

        if (
            homeassistant.get("device_class") != "energy"
            or homeassistant.get("state_class") != "total_increasing"
        ):
            raise ValidationError(
                "modbus.resets only applies to an energy counter, so it requires "
                "homeassistant.device_class: energy and "
                "homeassistant.state_class: total_increasing"
            )
