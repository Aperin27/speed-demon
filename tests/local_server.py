"""Browser smoke server using only in-memory Firebase fakes. Never accesses Firebase.

Run from the repository root: python -B -m tests.local_server
"""
import tempfile
import threading
from pathlib import Path

import uvicorn

from backend import main
from backend.firebase_service import board_key
from backend.persistence import ResultOutbox


class LocalFirebase:
    def __init__(self):
        self.records = {}
        self.boards = {}
        self.lock = threading.Lock()

    def verify_token(self, token):
        raise ValueError("Real credentials are never accepted by the smoke server")

    def save_round(self, record):
        with self.lock:
            if record["round_id"] in self.records:
                return
            self.records[record["round_id"]] = record
            for result in record["results"]:
                for period in ("weekly", "all_time"):
                    key = board_key(record["duration"], period, record["week"])
                    board = self.boards.setdefault(key, {})
                    if result["score"] > board.get(result["uid"], {}).get("score", -1):
                        board[result["uid"]] = {**result, "achieved_at": record["completed_at"]}

    def leaderboard(self, duration, period, week):
        with self.lock:
            rows = sorted(self.boards.get(board_key(duration, period, week), {}).values(),
                          key=lambda row: (-row["score"], row["achieved_at"], row["uid"]))[:10]
            return [{"name": row["name"], "score": row["score"]} for row in rows]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="speed-demon-smoke-") as directory:
        main.firebase = LocalFirebase()
        main.outbox = ResultOutbox(str(Path(directory) / "results.sqlite3"), main.firebase)
        uvicorn.run(main.app, host="127.0.0.1", port=8000, ws_max_size=16384)
