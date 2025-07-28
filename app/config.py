from marshmallow import Schema, fields, validates_schema, ValidationError


class HttpConfig(Schema):
    enabled = fields.Bool(required=False, load_default=True)
    user = fields.Str(required=False, load_default="admin")
    password = fields.Str(required=False, load_default="123456789")

    @validates_schema()
    def validate_user_requires_password(self, data, **kwargs):
        if "user" in data and "password" not in data:
            raise ValidationError("Password must be provided with username for HTTP")


class DataLoggerConfig(Schema):
    host = fields.Str(required=True)
    port = fields.Int(required=False, load_default=502)
    device_id = fields.Int(required=False, load_default=1)
    poll_interval = fields.Int(required=False, load_default=60)
    poll_interval_if_off = fields.Int(required=False, load_default=600)
    poll_retries = fields.Int(required=False, load_default=10)
    register_chunks = fields.Int(required=False, load_default=80)
    http = fields.Nested(HttpConfig(), required=False)


class InverterConfig(Schema):
    name = fields.Str(required=False, load_default="")
    manufacturer = fields.Str(required=False, load_default="")
    model = fields.Str(required=False, load_default="")


class MqttConfig(Schema):
    enabled = fields.Bool(required=True)
    host = fields.Str(required=False, allow_none=True, load_default="")
    port = fields.Int(required=False, load_default=1883)
    topic_prefix = fields.Str(required=False, load_default="tcpsolis2mqtt")
    client_id = fields.Str(required=False, load_default="tcpsolis2mqtt")
    user = fields.Str(required=False)
    password = fields.Str(required=False)
    use_ssl = fields.Bool(required=False, load_default=False)
    validate_cert = fields.Bool(required=False, load_default=False)

    @validates_schema()
    def validate_user_requires_password(self, data, **kwargs):
        if "user" in data and "password" not in data:
            raise ValidationError("Password must be provided with username for MQTT")

    @validates_schema()
    def require_host_if_enabled(self, data, **kwargs):
        if data["enabled"] and ("host" not in data or not data["host"]):
            raise ValidationError("Host is required if MQTT is enabled")


class AppConfig(Schema):
    debug = fields.Bool(load_default=False)
    datalogger = fields.Nested(DataLoggerConfig(), required=True)
    inverter = fields.Nested(InverterConfig(), required=False)
    mqtt = fields.Nested(MqttConfig(), required=True)
