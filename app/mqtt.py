from paho.mqtt import client as mqtt_client
from threading import Event
from time import monotonic, sleep
import logging

ONLINE = "online"
OFFLINE = "offline"


class Mqtt(mqtt_client.Client):
    def __init__(self, config):
        super().__init__(
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=config["client_id"],
            clean_session=True,
        )
        self.enable_logger()
        self.username_pw_set(config["user"], config["password"])
        if config["use_ssl"]:
            self.tls_set()
        if config["use_ssl"] and not config["validate_cert"]:
            self.tls_insecure_set(True)
        self.availability_topic = f"{config['topic_prefix']}/availability"

        # The broker publishes this if the connection drops without a clean
        # disconnect, which is the only thing that stops a crashed container from
        # looking alive. Home Assistant keeps showing the last state it was told
        # about, so without a will an active_power reading would sit there forever.
        # It has to be set before connecting.
        self.will_set(self.availability_topic, OFFLINE, retain=True)

        # These have to be named something paho does not already define. A method
        # called on_connect shadows paho's property of the same name, so the property
        # setter never runs and _on_connect stays None. paho reads the public
        # attribute, so a shadowed method does still fire, but nothing about that is
        # worth relying on.
        self.on_connect = self._handle_connect
        self.on_disconnect = self._handle_disconnect
        self.connect(config["host"], config["port"])
        self.loop_start()

    def __del__(self):
        self.disconnect()

    def wait_until_connected(self, timeout=5):
        deadline = monotonic() + timeout

        while not self.is_connected() and monotonic() < deadline:
            sleep(0.1)

        return self.is_connected()

    def read_retained(self, topic, timeout=5):
        # Retained topics are the only storage this app has, so they double as a place
        # to keep state across restarts. A broker delivers a retained message as soon as
        # we subscribe, so if nothing arrives within the timeout there is no stored state.
        if not self.wait_until_connected(timeout):
            logging.error("MQTT not connected, unable to read %s", topic)
            return None

        payload = None
        received = Event()

        def on_message(client, userdata, message):
            nonlocal payload
            payload = message.payload.decode()
            received.set()

        self.on_message = on_message
        self.subscribe(topic)

        if not received.wait(timeout):
            logging.info("MQTT no retained message on %s", topic)

        self.unsubscribe(topic)
        self.on_message = None

        return payload

    def _handle_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logging.info("MQTT Failed to connect, return code: %s", reason_code)
            return

        logging.info("MQTT Connected to Broker!")

        # On every connect, not just the first: a reconnect follows the broker having
        # published the will, so the topic has to be put back.
        self.publish(self.availability_topic, ONLINE, retain=True)

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties):
        # Nothing to do but say so. loop_start runs a network thread that reconnects
        # on its own, and the broker publishes the will meanwhile.
        logging.info("MQTT Disconnected with result code: %s", reason_code)
