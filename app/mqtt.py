from paho.mqtt import client as mqtt_client
from time import sleep
import logging


class Mqtt(mqtt_client.Client):
    def __init__(self, config):
        super().__init__(client_id=config.client_id, clean_session=True)
        self.enable_logger()
        self.username_pw_set(config.user, config.password)
        if config.use_ssl:
            self.tls_set()
        if config.use_ssl and not config.validate_cert:
            self.tls_insecure_set(True)
        self.on_connect = self.on_connect
        self.on_disconnect = self.on_disconnect
        self.connect(config.host, config.port)
        self.loop_start()

    def __del__(self):
        self.disconnect()

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logging.info("MQTT Connected to Broker!")
        elif rc == 1:
            logging.info("MQTT Connection refused - incorrect protocol version")
        elif rc == 2:
            logging.info("MQTT Connection refused - invalid client identifier")
        elif rc == 3:
            logging.info("MQTT Connection refused - server unavailable")
        elif rc == 4:
            logging.info("MQTT Connection refused - bad username or password")
        elif rc == 5:
            logging.info("MQTT Connection refused - not authorised")
        else:
            logging.info("MQTT Failed to connect, return code %d\n", rc)

    def on_disconnect(self, client, userdata, rc):
        logging.info("MQTT Disconnected with result code: %s", rc)
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
