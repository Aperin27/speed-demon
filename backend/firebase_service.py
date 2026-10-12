"""Lazy Firebase access for verified identities, nickname claims and rankings."""
import hashlib
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "speed-demon-d6581")
SCORING_VERSION = "arithmetic-v3"
DURATIONS = (60, 120, 300, 600)
JOHANNESBURG = timezone(timedelta(hours=2))
RESERVED_USERNAMES = {"admin", "administrator", "moderator", "system", "support"}


class UsernameError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def validate_username(username):
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username, re.ASCII):
        raise UsernameError("Use 3–20 ASCII letters, numbers or underscores.", "invalid_username")
    if username.lower() in RESERVED_USERNAMES:
        raise UsernameError("This username is reserved.", "reserved_username")
    return normalize_nickname(username)


def profile_key(uid):
    return hashlib.sha256(uid.encode()).hexdigest()


def normalize_nickname(nickname):
    return nickname.strip().casefold()


def week_key(timestamp):
    local = datetime.fromtimestamp(timestamp, JOHANNESBURG)
    return (local.date() - timedelta(days=local.weekday())).isoformat()


def board_key(duration, period, week=None, version=SCORING_VERSION):
    return f"{version}_{duration}_{week if period == 'weekly' else 'all'}"


def public_name(value):
    return value.strip()[:40] if isinstance(value, str) and "@" not in value and value.strip() else "Player"


class FirebaseService:
    def nickname_reserved(self, nickname):
        normalized = normalize_nickname(nickname)
        if "/" in normalized or normalized in (".", ".."):
            raise ValueError("Nickname cannot be checked as a Firestore reservation")
        self.initialize()
        return self._db.collection("usernames").document(normalized).get(timeout=10).exists

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
        return {"uid": claims["uid"], "username": self.get_username(claims["uid"]), "exp": claims["exp"]}

    def get_username(self, uid):
        self.initialize()
        snapshot = self._db.collection("profiles").document(profile_key(uid)).get(timeout=10)
        return snapshot.to_dict()["username"] if snapshot.exists else None

    def register_username(self, uid, username):
        normalized = validate_username(username)
        self.initialize()
        from firebase_admin import firestore
        owner = profile_key(uid)
        profile_ref = self._db.collection("profiles").document(owner)
        claim_ref = self._db.collection("usernames").document(normalized)

        @firestore.transactional
        def claim(transaction):
            profile = profile_ref.get(transaction=transaction)
            if profile.exists:
                existing = profile.to_dict()["username"]
                if existing.lower() == normalized:
                    return existing
                raise UsernameError("Your username is permanent and cannot be changed.", "username_permanent")
            reservation = claim_ref.get(transaction=transaction)
            if reservation.exists:
                raise UsernameError("This username is already registered.", "username_taken")
            transaction.create(profile_ref, {"username": username, "normalized_username": normalized, "created_at": time.time()})
            transaction.create(claim_ref, {"profile_id": owner})
            return username

        return claim(self._db.transaction(max_attempts=5))

    def save_round(self, record):
        self.initialize()
        from firebase_admin import firestore
        database = self._db
        round_ref = database.collection("rounds").document(record["round_id"])
        updates = []
        for result in record["results"]:
            player_key = profile_key(result["uid"])
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
