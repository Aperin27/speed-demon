import asyncio
import copy
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException
from backend import main
from backend.firebase_service import UsernameError, profile_key
from tests import test_persistence as fixtures

record = fixtures.record


class UsernameTests(unittest.TestCase):
    setUp = fixtures.FirestoreTests.setUp
    tearDown = fixtures.FirestoreTests.tearDown
    def test_reservation_lookup_normalizes_without_writing(self):
        self.service.register_username("owner", "MathRex")
        original = copy.deepcopy(self.database.documents)
        for nickname in ("MathRex", "MATHREX", " mathrex "):
            self.assertTrue(self.service.nickname_reserved(nickname))
        self.assertFalse(self.service.nickname_reserved("Available"))
        self.assertFalse(self.service.nickname_reserved("Guest name"))
        with self.assertRaises(ValueError):
            self.service.nickname_reserved("Guest / name")
        self.assertEqual(self.database.documents, original)

    def test_unique_permanent_username_and_idempotent_retry(self):
        self.assertIsNone(self.service.get_username("uid-a"))
        self.assertEqual(self.service.register_username("uid-a", "Speed_Player"), "Speed_Player")
        original = copy.deepcopy(self.database.documents)
        self.assertEqual(self.service.register_username("uid-a", "speed_player"), "Speed_Player")
        self.assertEqual(self.database.documents, original)
        self.assertEqual(self.service.get_username("uid-a"), "Speed_Player")
        with self.assertRaises(UsernameError) as error:
            self.service.register_username("uid-b", "SPEED_PLAYER")
        self.assertEqual(error.exception.code, "username_taken")
        with self.assertRaises(UsernameError) as error:
            self.service.register_username("uid-a", "Another_Name")
        self.assertEqual(error.exception.code, "username_permanent")
        self.assertEqual(self.database.documents, original)

    def test_invalid_and_reserved_names_never_write(self):
        for name in (None, True, "ab", "a" * 21, "space name", "éclair", "Ａdmin", "a-b", "Admin", "MODERATOR", "system", "support", "administrator", " abc"):
            with self.subTest(name=name), self.assertRaises(UsernameError):
                self.service.register_username("uid", name)
        self.assertEqual(self.database.documents, {})

    def race(self, claims):
        barrier = threading.Barrier(2)
        local = threading.local()

        def overlap_first_commit():
            if not getattr(local, "waited", False):
                local.waited = True
                barrier.wait(timeout=3)

        self.database.before_commit = overlap_first_commit

        def attempt(claim):
            try:
                return self.service.register_username(*claim)
            except UsernameError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, claims))
        self.database.before_commit = lambda: None
        return outcomes

    def test_simultaneous_case_insensitive_claims_have_one_winner(self):
        outcomes = self.race([("uid-a", "Fast_Player"), ("uid-b", "fast_player")])
        self.assertEqual(outcomes.count("username_taken"), 1)
        self.assertEqual(len(self.database.documents), 2)
        owner = self.database.documents["usernames/fast_player"]["profile_id"]
        self.assertIn(owner, (profile_key("uid-a"), profile_key("uid-b")))

    def test_one_uid_cannot_race_to_claim_two_usernames(self):
        outcomes = self.race([("uid-a", "First_Name"), ("uid-a", "Second_Name")])
        self.assertEqual(outcomes.count("username_permanent"), 1)
        self.assertEqual(len(self.database.documents), 2)

    def test_transaction_failure_does_not_partially_register(self):
        def fail():
            raise RuntimeError("Firestore unavailable")
        self.database.before_commit = fail
        with self.assertRaises(RuntimeError):
            self.service.register_username("uid", "New_Player")
        self.assertEqual(self.database.documents, {})
        self.database.before_commit = lambda: None
        self.assertEqual(self.service.register_username("uid", "New_Player"), "New_Player")

    def test_negative_best_scores_and_old_board_isolation(self):
        old = record("legacy", score=999)
        old["scoring_version"] = "arithmetic-v2"
        self.service.save_round(old)
        self.assertEqual(self.service.leaderboard(60, "all_time", None), [])
        self.service.save_round(record("negative", score=-5))
        self.service.save_round(record("worse", score=-7))
        self.assertEqual(self.service.leaderboard(60, "all_time", None)[0]["score"], -5)
        self.service.save_round(record("better", score=-2))
        self.assertEqual(self.service.leaderboard(60, "all_time", None)[0]["score"], -2)
        self.assertEqual(self.database.documents["rounds/legacy"], old)


class ProfileEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.security.limiter.clear()

    async def test_verified_uid_only_and_private_response(self):
        identity = {"uid": "verified-uid", "username": None, "exp": time.time() + 60}
        with patch.object(main.firebase, "verify_token", return_value=identity), patch.object(main.firebase, "register_username", return_value="New_Player") as register:
            profile = await main.get_profile("Bearer token")
            self.assertNotIn(b"verified-uid", profile.body)
            self.assertEqual(profile.headers["cache-control"], "no-store")
            response = await main.register_username({"username": "New_Player", "uid": "forged-uid"}, "Bearer token")
            register.assert_called_once_with("verified-uid", "New_Player")
            self.assertNotIn(b"uid", response.body)

    async def test_missing_expired_and_invalid_authentication(self):
        for token in (None, "Basic token", "Bearer " + "x" * 12001):
            with self.assertRaises(HTTPException) as error:
                await main.get_profile(token)
            self.assertEqual(error.exception.status_code, 401)
        with patch.object(main.firebase, "verify_token", return_value={"uid": "uid", "username": None, "exp": time.time() - 1}):
            with self.assertRaises(HTTPException):
                await main.register_username({"username": "Player"}, "Bearer expired")

    async def test_conflicts_and_storage_failures_are_sanitized(self):
        identity = {"uid": "uid", "username": None, "exp": time.time() + 60}
        for failure, status in [(UsernameError("Taken", "username_taken"), 409), (UsernameError("Invalid", "invalid_username"), 400), (RuntimeError("private credential path"), 503)]:
            with patch.object(main.firebase, "verify_token", return_value=identity), patch.object(main.firebase, "register_username", side_effect=failure):
                with self.assertRaises(HTTPException) as error:
                    await main.register_username({"username": "Player"}, "Bearer token")
                self.assertEqual(error.exception.status_code, status)
                self.assertNotIn("private", error.exception.detail)
