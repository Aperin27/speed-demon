"""Lazy Firebase access; guests don't require credentials or the Admin SDK."""
import hashlib
import os
import threading
from datetime import datetime, timedelta, timezone

PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "speed-demon-d6581")
SCORING_VERSION = "arithmetic-v2"
DURATIONS = (60, 120, 300, 600)
JOHANNESBURG = timezone(timedelta(hours=2))


def week_key(timestamp):
    local = datetime.fromtimestamp(timestamp, JOHANNESBURG)
    return (local.date() - timedelta(days=local.weekday())).isoformat()


def board_key(duration, period, week=None, version=SCORING_VERSION):
    return f"{version}_{duration}_{week if period == 'weekly' else 'all'}"


def public_name(value):
    return value.strip()[:40] if isinstance(value, str) and "@" not in value and value.strip() else "Player"


class FirebaseService:
    def __init__(self):
        self._app = None
        self._db = None
        self._lock = threading.Lock()

    def initialize(self):
        with self._lock:
            if self._app is None:
                if os.getenv("FIREBASE_AUTH_EMULATOR_HOST") or os.getenv("FIRESTORE_EMULATOR_HOST"):
                    raise RuntimeError("Emulator variables unsupported in this production service")
                import firebase_admin
                from firebase_admin import credentials, firestore
                path = os.getenv("FIREBASE_CREDENTIALS_FILE")
                credential = credentials.Certificate(path) if path else credentials.ApplicationDefault()
                app = firebase_admin.initialize_app(credential, {"projectId": PROJECT_ID, "httpTimeout": 10}, name="speed-demon")
                try:
                    db = firestore.client(app=app)
                except Exception:
                    firebase_admin.delete_app(app)
                    raise
                self._app, self._db = app, db
        return self._app

    def verify_token(self, token):
        from firebase_admin import auth
        claims = auth.verify_id_token(token, app=self.initialize(), check_revoked=True)
        if claims.get("firebase", {}).get("sign_in_provider") != "google.com":
            raise ValueError("Google sign-in required")
        return {"uid": claims["uid"], "name": public_name(claims.get("name")), "exp": claims["exp"]}

    def save_round(self, record):
        self.initialize()
        from firebase_admin import firestore
        database = self._db
        round_ref = database.collection("rounds").document(record["round_id"])
        updates = []
        for result in record["results"]:
            player_key = hashlib.sha256(result["uid"].encode()).hexdigest()
            for period in ("weekly", "all_time"):
                key = board_key(record["duration"], period, record["week"], record["scoring_version"])
                ref = database.collection("boards").document(key).collection("entries").document(player_key)
                updates.append((ref, result))

        @firestore.transactional
        def persist(transaction):
            if round_ref.get(transaction=transaction).exists:
                return
            snapshots = [(ref, result, ref.get(transaction=transaction)) for ref, result in updates]
            transaction.create(round_ref, record)
            for ref, result, snapshot in snapshots:
                previous = snapshot.to_dict() if snapshot.exists else None
                candidate = (-result["score"], record["completed_at"], record["round_id"])
                old = (-previous["score"], previous["achieved_at"], previous["round_id"]) if previous else None
                if old is None or candidate < old:
                    transaction.set(ref, {**result, "achieved_at": record["completed_at"], "round_id": record["round_id"]})
        persist(database.transaction(max_attempts=5))

    def leaderboard(self, duration, period, week):
        self.initialize()
        from google.cloud.firestore_v1 import Query
        query = self._db.collection("boards").document(board_key(duration, period, week)).collection("entries")
        query = query.order_by("score", direction=Query.DESCENDING).order_by("achieved_at").order_by("uid").limit(10)
        return [{"name": row["name"], "score": row["score"]} for snapshot in query.stream(timeout=10)
                for row in [snapshot.to_dict()]]


firebase = FirebaseService()
