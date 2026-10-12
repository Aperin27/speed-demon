"""Browser smoke server using only in-memory Firebase fakes. Never accesses Firebase.

Run from the repository root: python -B -m tests.local_server
"""
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from backend import main
from backend.firebase_service import UsernameError, board_key, normalize_nickname, validate_username
from backend.persistence import ResultOutbox


class LocalFirebase:
    def __init__(self):
        self.records = {}
        self.boards = {}
        self.profiles = {}
        self.usernames = {}
        self.lock = threading.Lock()

    def verify_token(self, token):
        if token not in ("local-smoke-account-a", "local-smoke-account-b"):
            raise ValueError("Real credentials are never accepted by the smoke server")
        return {"uid": token, "username": self.get_username(token), "exp": time.time() + 3600}

    def get_username(self, uid):
        with self.lock:
            return self.profiles.get(uid)

    def nickname_reserved(self, nickname):
        with self.lock:
            return normalize_nickname(nickname) in self.usernames

    def register_username(self, uid, username):
        normalized = validate_username(username)
        with self.lock:
            existing = self.profiles.get(uid)
            if existing:
                if existing.lower() == normalized:
                    return existing
                raise UsernameError("Your username is permanent and cannot be changed.", "username_permanent")
            if normalized in self.usernames:
                raise UsernameError("This username is already registered.", "username_taken")
            self.profiles[uid] = username
            self.usernames[normalized] = uid
            return username

    def save_round(self, record):
        with self.lock:
            if record["round_id"] in self.records:
                return
            self.records[record["round_id"]] = record
            for result in record["results"]:
                for period in ("weekly", "all_time"):
                    key = board_key(record["duration"], period, record["week"], record["scoring_version"])
                    board = self.boards.setdefault(key, {})
                    if result["uid"] not in board or result["score"] > board[result["uid"]]["score"]:
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
