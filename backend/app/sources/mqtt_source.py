"""REAL data source: ESP32 over WiFi -> MQTT broker -> backend.

Topics:  <prefix>/<device>/ecg   and   <prefix>/<device>/vitals   (JSON payloads, see protocol.py)
"""
from __future__ import annotations

import logging
import time

import paho.mqtt.client as mqtt

from ..protocol import ProtocolError, parse_message

log = logging.getLogger("mqtt")


class MqttSource:
    def __init__(self, settings, pipeline):
        self.s = settings
        self.pipeline = pipeline
        self.connected = False
        self.last_rx = 0.0
        self.last_error = ""
        try:  # paho-mqtt >= 2.0
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="healthmon-backend")
        except AttributeError:  # paho-mqtt 1.x
            self.client = mqtt.Client(client_id="healthmon-backend")
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        if settings.mqtt_tls:
            self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 15)

    def start(self, loop) -> None:
        try:
            self.client.connect_async(self.s.mqtt_host, self.s.mqtt_port, keepalive=30)
            self.client.loop_start()
            log.info("REAL mode: MQTT %s:%d topics %s/+/(ecg|vitals)", self.s.mqtt_host, self.s.mqtt_port, self.s.mqtt_topic_prefix)
        except Exception as e:
            self.last_error = str(e)
            log.error("MQTT start failed: %s", e)

    def stop(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def status(self) -> dict:
        return {"type": "mqtt", "connected": self.connected, "broker": f"{self.s.mqtt_host}:{self.s.mqtt_port}",
                "last_rx_age_s": round(time.time() - self.last_rx, 1) if self.last_rx else None,
                "last_error": self.last_error}

    def _on_connect(self, client, userdata, *args):
        rc = args[1] if len(args) >= 2 else (args[0] if args else 0)
        ok = (getattr(rc, "is_failure", None) is False) or rc == 0
        self.connected = bool(ok)
        if ok:
            p = self.s.mqtt_topic_prefix
            client.subscribe([(f"{p}/+/ecg", 0), (f"{p}/+/vitals", 0)])
            self.last_error = ""
            log.info("MQTT connected")
        else:
            self.last_error = f"connect refused: {rc}"
            log.warning(self.last_error)

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        log.warning("MQTT disconnected")

    def _on_message(self, client, userdata, message):
        parts = message.topic.split("/")
        topic_dev = parts[-2] if len(parts) >= 2 else None
        try:
            msg = parse_message(message.payload, default_dev=self.s.device_id, topic_dev=topic_dev)
        except ProtocolError as e:
            self.pipeline.stats["parse_errors"] += 1
            log.debug("bad MQTT payload: %s", e)
            return
        if msg is None:
            return
        self.last_rx = time.time()
        self.pipeline.submit_threadsafe(msg)
