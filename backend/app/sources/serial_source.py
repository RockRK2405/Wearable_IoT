"""REAL data source: ESP32 over USB Serial (JSON lines, see protocol.py)."""
from __future__ import annotations

import logging
import threading
import time

import serial
from serial.tools import list_ports

from ..protocol import ProtocolError, parse_message

log = logging.getLogger("serial")
# USB-serial bridges commonly found on ESP32 dev boards: CP210x, CH340/CH341, FTDI, Espressif native USB
KNOWN_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}


def find_port() -> str | None:
    ports = list(list_ports.comports())
    for p in ports:
        if p.vid in KNOWN_VIDS:
            return p.device
    for p in ports:
        if "USB" in (p.device or "").upper() or "ACM" in (p.device or "").upper():
            return p.device
    return None


class SerialSource:
    def __init__(self, settings, pipeline):
        self.s = settings
        self.pipeline = pipeline
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.port = None
        self.last_rx = 0.0
        self.last_error = ""

    def start(self, loop) -> None:
        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"type": "serial", "connected": self.connected, "port": self.port,
                "baud": self.s.serial_baud, "last_rx_age_s": round(time.time() - self.last_rx, 1) if self.last_rx else None,
                "last_error": self.last_error}

    def _run(self) -> None:
        while not self._stop.is_set():
            port = self.s.serial_port if self.s.serial_port.lower() != "auto" else find_port()
            if not port:
                self.last_error = "no serial port found - plug in the ESP32 or set SERIAL_PORT"
                log.warning(self.last_error + " (available: %s)", [p.device for p in list_ports.comports()])
                self._stop.wait(3)
                continue
            try:
                with serial.Serial(port, self.s.serial_baud, timeout=1) as ser:
                    self.port, self.connected, self.last_error = port, True, ""
                    log.info("REAL mode: reading %s @ %d baud", port, self.s.serial_baud)
                    try:
                        ser.reset_input_buffer()
                    except Exception:
                        pass
                    self._read_loop(ser)
            except (serial.SerialException, OSError) as e:
                self.last_error = str(e)
                log.warning("serial error: %s - retrying", e)
            self.connected = False
            self._stop.wait(3)

    def _read_loop(self, ser) -> None:
        while not self._stop.is_set():
            line = ser.readline()
            if not line:
                continue
            try:
                msg = parse_message(line, default_dev=self.s.device_id)
            except ProtocolError as e:
                self.pipeline.stats["parse_errors"] += 1
                log.debug("bad line: %s (%r)", e, line[:80])
                continue
            if msg is None:
                continue
            self.last_rx = time.time()
            self.pipeline.submit_threadsafe(msg)
