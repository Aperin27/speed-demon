"""SQLite outbox and idempotent Firestore writes, isolated from gameplay."""
import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .firebase_service import firebase

logger = logging.getLogger(__name__)


class ResultOutbox:
    def __init__(self, path=None, service=None):
        self.path = path or os.getenv("RESULT_OUTBOX_PATH", str(Path(__file__).parent / "data" / "results.sqlite3"))
        self.service = service or firebase
        self.task = None
        self.wake = asyncio.Event()

    @contextmanager
    def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS pending (round_id TEXT PRIMARY KEY, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0)")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def enqueue(self, record):
        with self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO pending (round_id, payload) VALUES (?, ?)", (record["round_id"], json.dumps(record)))
        self.wake.set()

    async def process_once(self):
        with self.connect() as connection:
            row = connection.execute("SELECT round_id, payload, attempts FROM pending WHERE retry_at <= ? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
        if row is None:
            return False
        round_id, payload, attempts = row
        try:
            await asyncio.to_thread(self.service.save_round, json.loads(payload))
        except Exception:
            logger.warning("Leaderboard persistence unavailable; result retained for retry")
            with self.connect() as connection:
                connection.execute("UPDATE pending SET attempts=?, retry_at=? WHERE round_id=?", (attempts + 1, time.time() + min(300, 2 ** min(attempts + 1, 8)), round_id))
        else:
            with self.connect() as connection:
                connection.execute("DELETE FROM pending WHERE round_id=?", (round_id,))
        return True

    async def run(self):
        while True:
            self.wake.clear()
            try:
                processed = await self.process_once()
            except Exception:
                logger.error("Result retry queue unavailable; retrying")
                processed = False
            if processed:
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), 5)
            except asyncio.TimeoutError:
                pass

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


outbox = ResultOutbox()
