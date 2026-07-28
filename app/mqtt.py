from paho.mqtt import client as mqtt_client
from threading import Event
from time import monotonic, sleep
import logging


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
        self.on_connect = self.on_connect
        self.on_disconnect = self.on_disconnect
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

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logging.info("MQTT Connected to Broker!")
        else:
            logging.info("MQTT Failed to connect, return code: %s", reason_code)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        logging.info("MQTT Disconnected with result code: %s", reason_code)
        reconnect_count, reconnect_delay = 0, 1
        while reconnect_count < 12:
            logging.info("MQTT Reconnecting in %d seconds...", reconnect_delay)
            sleep(reconnect_delay)

            try:
                client.reconnect()
                logging.info("MQTT Reconnected successfully!")
                return
            except Exception as err:
                logging.error("MQTT %s. Reconnect failed. Retrying...", err)

            reconnect_delay *= 2
            reconnect_delay = min(reconnect_delay, 60)
            reconnect_count += 1
        logging.info(
            "MQTT Reconnect failed after %s attempts. Exiting...", reconnect_count
        )
