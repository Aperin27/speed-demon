import copy
import asyncio
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend.firebase_service import FirebaseService, SCORING_VERSION, board_key, public_name, week_key
from backend.persistence import ResultOutbox


class Snapshot:
    def __init__(self, value):
        self.exists = value is not None
        self.value = copy.deepcopy(value)

    def to_dict(self):
        return self.value


class Reference:
    def __init__(self, database, path):
        self.database, self.path = database, path

    def collection(self, name):
        return Reference(self.database, self.path + "/" + name)

    def document(self, name):
        return Reference(self.database, self.path + "/" + name)

    def get(self, transaction):
        assert not transaction.writes, "Firestore reads must precede all writes"
        return Snapshot(self.database.documents.get(self.path))

    def order_by(self, field, direction="ASCENDING"):
        self.database.orders.append((field, direction))
        return self

    def limit(self, count):
        self.database.limit = count
        return self

    def stream(self, timeout):
        assert timeout == 10
        rows = [value for key, value in self.database.documents.items() if key.startswith(self.path + "/")]
        rows.sort(key=lambda row: (-row["score"], row["achieved_at"], row["uid"]))
        return [Snapshot(row) for row in rows[:self.database.limit]]


class Transaction:
    def __init__(self, database):
        self.database = database
        self.writes = []

    def create(self, reference, record):
        assert reference.path not in self.database.documents
        self.writes.append((reference.path, copy.deepcopy(record)))

    def set(self, reference, record):
        self.writes.append((reference.path, copy.deepcopy(record)))


class FakeFirestore:
    def __init__(self):
        self.documents = {}
        self.orders = []
        self.limit = None

    def collection(self, name):
        return Reference(self, name)

    def transaction(self, max_attempts):
        assert max_attempts == 5
        return Transaction(self)

    @staticmethod
    def transactional(function):
        def run(transaction):
            function(transaction)
            for path, record in transaction.writes:
                transaction.database.documents[path] = record
        return run


def record(round_id="round-a", score=5, duration=60, completed_at=200):
    return {"round_id": round_id, "scoring_version": SCORING_VERSION, "duration": duration,
            "started_at": 100, "completed_at": completed_at, "week": "2026-10-05", "player_count": 2,
            "results": [{"uid": "verified-uid", "name": "Player", "score": score}]}


class FirestoreTests(unittest.TestCase):
    def setUp(self):
        self.service = FirebaseService()
        self.database = FakeFirestore()
        self.service._db = self.database
        self.service.initialize = lambda: "fake-app"
        admin = types.ModuleType("firebase_admin")
        admin.firestore = types.SimpleNamespace(transactional=FakeFirestore.transactional)
        query = types.ModuleType("google.cloud.firestore_v1")
        query.Query = types.SimpleNamespace(DESCENDING="DESCENDING")
        self.modules = patch.dict(sys.modules, {"firebase_admin": admin, "google.cloud.firestore_v1": query})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()

    def test_actual_transaction_idempotency_best_score_and_read_before_write(self):
        self.service.save_round(record())
        original = copy.deepcopy(self.database.documents)
        self.service.save_round(record(score=999))
        self.assertEqual(self.database.documents, original)
        self.service.save_round(record("round-b", score=3))
        self.assertEqual(self.service.leaderboard(60, "all_time", None), [{"name": "Player", "score": 5}])
        self.service.save_round(record("round-c", score=7))
        self.assertEqual(self.service.leaderboard(60, "weekly", "2026-10-05")[0]["score"], 7)
        self.assertEqual(len([key for key in self.database.documents if key.startswith("rounds/")]), 3)

    def test_duration_week_version_isolation_and_stable_top_ten(self):
        self.service.save_round(record(duration=120))
        self.assertEqual(self.service.leaderboard(60, "weekly", "2026-10-05"), [])
        self.assertEqual(self.service.leaderboard(120, "weekly", "2026-10-12"), [])
        self.assertIn(SCORING_VERSION, board_key(60, "weekly", "2026-10-05"))
        for index in reversed(range(12)):
            result = record(f"round-{index}", score=10)
            result["results"][0]["uid"] = f"uid-{index:02d}"
            result["results"][0]["name"] = f"Player {index}"
            self.service.save_round(result)
        entries = self.service.leaderboard(60, "all_time", None)
        self.assertEqual(len(entries), 10)
        self.assertEqual(entries[0]["name"], "Player 0")
        self.assertEqual(self.database.orders[-3:], [("score", "DESCENDING"), ("achieved_at", "ASCENDING"), ("uid", "ASCENDING")])

    def test_equal_best_preserves_earliest_achievement(self):
        self.service.save_round(record(completed_at=200))
        self.service.save_round(record("later", completed_at=300))
        entries = [value for key, value in self.database.documents.items() if "/entries/" in key]
        self.assertTrue(all(value["achieved_at"] == 200 for value in entries))
        self.service.save_round(record("delayed-earlier", completed_at=150))
        entries = [value for key, value in self.database.documents.items() if "/entries/" in key]
        self.assertTrue(all(value["achieved_at"] == 150 for value in entries))

    def test_retry_of_old_scoring_version_cannot_enter_current_rankings(self):
        older = record("old-version", score=999)
        older["scoring_version"] = "arithmetic-v1"
        self.service.save_round(older)
        self.assertEqual(self.service.leaderboard(60, "all_time", None), [])
        self.assertTrue(any("arithmetic-v1_60_all" in key for key in self.database.documents))
        self.service.save_round(record("current-version", score=2))
        self.assertEqual(self.service.leaderboard(60, "all_time", None)[0]["score"], 2)

    def test_verified_google_token_and_no_email_fallback(self):
        auth = types.SimpleNamespace(verify_id_token=Mock(return_value={
            "uid": "verified", "exp": 1000, "email": "secret@example.com",
            "firebase": {"sign_in_provider": "google.com"}}))
        sys.modules["firebase_admin"].auth = auth
        identity = self.service.verify_token("opaque-token")
        auth.verify_id_token.assert_called_once_with("opaque-token", app="fake-app", check_revoked=True)
        self.assertEqual(identity, {"uid": "verified", "name": "Player", "exp": 1000})
        auth.verify_id_token.return_value["firebase"]["sign_in_provider"] = "password"
        with self.assertRaises(ValueError):
            self.service.verify_token("opaque-token")
        for failure in (ValueError("expired"), ValueError("revoked"), ValueError("invalid signature")):
            auth.verify_id_token.side_effect = failure
            with self.assertRaises(ValueError):
                self.service.verify_token("opaque-token")
        self.assertEqual(public_name("secret@example.com"), "Player")


class WeekTests(unittest.TestCase):
    def test_monday_midnight_johannesburg_utc_boundary_and_new_year(self):
        timestamp = datetime(2026, 10, 11, 22, tzinfo=timezone.utc).timestamp()
        self.assertEqual(week_key(timestamp - .001), "2026-10-05")
        self.assertEqual(week_key(timestamp), "2026-10-12")
        self.assertEqual(week_key(timestamp + .001), "2026-10-12")
        self.assertEqual(week_key(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()), "2026-12-28")


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_recovers_after_temporary_local_queue_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = ResultOutbox(str(Path(directory) / "results.sqlite3"), Mock())
            with patch.object(queue, "process_once", new=AsyncMock(side_effect=[OSError("locked"), False])) as process:
                queue.start()
                await asyncio.sleep(.01)
                queue.wake.set()
                await asyncio.sleep(.01)
                await queue.stop()
                self.assertEqual(process.call_count, 2)

    async def test_failure_retry_restart_and_duplicate_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "results.sqlite3")
            service = Mock()
            service.save_round.side_effect = RuntimeError("offline")
            queue = ResultOutbox(path, service)
            queue.enqueue(record())
            queue.enqueue(record())
            await queue.process_once()
            with queue.connect() as database:
                rows = database.execute("SELECT attempts, retry_at FROM pending").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], 1)
                self.assertGreater(rows[0][1], time.time())
            self.assertFalse(await queue.process_once())
            recovered = ResultOutbox(path, service)
            service.save_round.side_effect = None
            with recovered.connect() as database:
                database.execute("UPDATE pending SET retry_at=0")
            self.assertTrue(await recovered.process_once())
            self.assertFalse(await recovered.process_once())
            self.assertEqual(service.save_round.call_count, 2)
            with recovered.connect() as database:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM pending").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
