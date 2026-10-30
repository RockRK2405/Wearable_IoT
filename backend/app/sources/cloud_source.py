"""CLOUD data source.

In cloud mode the ESP32 (or any client) pushes readings to the backend over the
internet with HTTP POST /api/ingest, so there is no background reader thread to
run here. This source only tracks a little ingest status for the dashboard; the
actual message handling happens in the /api/ingest endpoint, which calls
pipeline.submit_threadsafe(...).
"""
from __future__ import annotations

import time

from ..settings import Settings


class CloudSource:
    def __init__(self, settings: Settings, pipeline):
        self.s = settings
        self.pipeline = pipeline
        self.started_at = 0.0
        self.last_ingest_at = 0.0
        self.ingest_count = 0
        self.last_device = ""

    def start(self, loop) -> None:
        self.started_at = time.time()

    def stop(self) -> None:
        pass

    def note_ingest(self, device: str) -> None:
        self.last_ingest_at = time.time()
        self.ingest_count += 1
        self.last_device = device

    def status(self) -> dict:
        now = time.time()
        return {
            "transport": "cloud-http",
            "endpoint": "/api/ingest",
            "ingest_count": self.ingest_count,
            "last_device": self.last_device,
            "last_ingest_age_s": round(now - self.last_ingest_at, 1) if self.last_ingest_at else None,
            "token_required": bool(self.s.ingest_token),
        }
