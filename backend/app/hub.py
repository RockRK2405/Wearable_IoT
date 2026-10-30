"""Fan-out of JSON events to WebSocket clients (one bounded queue per client)."""
from __future__ import annotations

import asyncio
import json


class Hub:
    def __init__(self, qsize: int = 500):
        self.qsize = qsize
        self.clients: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.qsize)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def publish(self, msg: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(msg, separators=(",", ":"), default=str)
        for q in list(self.clients):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:  # slow client: drop its oldest message
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except Exception:
                    pass
