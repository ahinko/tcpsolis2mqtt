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
