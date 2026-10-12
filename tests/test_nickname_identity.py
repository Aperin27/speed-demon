import asyncio
import time
import unittest
from unittest.mock import patch

from backend import main
from tests.local_server import LocalFirebase
from tests.test_round_flow import Client


class NicknameIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.rooms.clear()
        main.security.limiter.clear()
        self.firebase = LocalFirebase()
        self.firebase_patch = patch.object(main, "firebase", self.firebase)
        self.firebase_patch.start()
        self.players = []

    async def asyncTearDown(self):
        for room in main.rooms.values():
            if room.get("task"):
                room["task"].cancel()
                await asyncio.gather(room["task"], return_exceptions=True)
        main.rooms.clear()
        self.firebase_patch.stop()

    def player(self, username=None, uid=None):
        player = {"id": str(len(self.players)), "websocket": Client(), "nickname": "", "room": None,
                  "identity": {"uid": uid, "username": username, "exp": time.time() + 3600} if uid else None,
                  "last_auth": -float("inf"), "closed": False, "send_lock": asyncio.Lock()}
        self.players.append(player)
        return player

    async def enter(self, player, nickname, code=None):
        await main.handle_message(player, {"type": "join_room" if code else "create_room", "nickname": nickname, "room_code": code})
        return player["room"]

    async def test_guests_and_google_unregistered_cannot_enter_reserved_names(self):
        self.firebase.register_username("owner", "MathRex")
        code = await self.enter(self.player(), "Available")
        for uid in (None, "google-unregistered"):
            for name in ("MathRex", " MATHREX ", "mathrex"):
                for room in (None, code):
                    player = self.player(uid=uid)
                    with self.subTest(uid=uid, name=name, room=room), self.assertRaisesRegex(ValueError, "belongs to a registered player"):
                        await self.enter(player, name, room)
                    self.assertIsNone(player["room"])
        self.assertEqual(self.firebase.usernames, {"mathrex": "owner"})

    async def test_available_names_preserve_nickname_and_reject_case_duplicates(self):
        guest = self.player(uid="unregistered")
        code = await self.enter(guest, " Available ")
        self.assertEqual(guest["nickname"], "Available")
        self.assertEqual(main.lobby_message(main.rooms[code], "test")["players"][0]["display_name"], "Guest · Available")
        with self.assertRaisesRegex(ValueError, "already in use"):
            await self.enter(self.player(), "AVAILABLE", code)
        await self.enter(self.player(), "Other", code)
        self.assertEqual(self.firebase.usernames, {})

    async def test_registered_canonical_identity_bypasses_guest_lookup(self):
        self.firebase.register_username("owner", "MathRex")
        owner = self.player("MathRex", "owner")
        with patch.object(self.firebase, "nickname_reserved", side_effect=RuntimeError("outage")):
            code = await self.enter(owner, "Forged_Name")
        self.assertEqual(owner["nickname"], "MathRex")
        self.assertEqual(main.lobby_message(main.rooms[code], "test")["players"][0]["display_name"], "MathRex")

    async def test_lookup_failure_fails_closed_on_create_join_and_start(self):
        host = self.player()
        code = await self.enter(host, "Host")
        await self.enter(self.player(), "Other", code)
        with patch.object(self.firebase, "nickname_reserved", side_effect=RuntimeError("private storage details")):
            for room in (None, code):
                with self.assertRaisesRegex(ValueError, "^Nickname verification unavailable. Try again.$"):
                    await self.enter(self.player(), "Available", room)
            with self.assertRaisesRegex(ValueError, "verification unavailable"):
                await main.handle_message(host, {"type": "start_game", "room_code": code, "duration": 60})
        self.assertEqual(main.rooms[code]["state"], "lobby")

    async def test_registration_after_guest_entry_revalidated_at_start(self):
        guest = self.player()
        code = await self.enter(guest, "MathRex")
        self.firebase.register_username("owner", "MathRex")
        await self.enter(self.player("MathRex", "owner"), "ignored", code)
        names = [p["display_name"] for p in main.lobby_message(main.rooms[code], "test")["players"]]
        self.assertEqual(names, ["Guest · MathRex", "MathRex"])
        with self.assertRaisesRegex(ValueError, "belongs to a registered player"):
            await main.handle_message(guest, {"type": "start_game", "room_code": code, "duration": 60})
        self.assertEqual(main.rooms[code]["state"], "lobby")

    async def test_claim_committing_after_lookup_stays_distinct_and_next_start_blocks(self):
        guest = self.player()
        lookup = self.firebase.nickname_reserved
        def claim_after_read(name):
            reserved = lookup(name)
            self.firebase.register_username("owner", "MathRex")
            return reserved
        with patch.object(main.firebase, "nickname_reserved", side_effect=claim_after_read):
            # Simulate a claim committing immediately after a successful reservation read.
            code = await self.enter(guest, "MathRex")
        await self.enter(self.player("MathRex", "owner"), "ignored", code)
        self.assertEqual(main.display_name(guest), "Guest · MathRex")
        with self.assertRaisesRegex(ValueError, "belongs to a registered player"):
            await main.handle_message(guest, {"type": "start_game", "room_code": code, "duration": 60})

    async def test_active_round_registration_keeps_snapshot_and_no_answer_reads(self):
        guest = self.player()
        code = await self.enter(guest, "MathRex")
        await self.enter(self.player(), "Other", code)
        with patch.object(self.firebase, "nickname_reserved", wraps=self.firebase.nickname_reserved) as lookup:
            await main.handle_message(guest, {"type": "start_game", "room_code": code, "duration": 60})
            self.assertEqual(lookup.call_count, 2)
            self.firebase.register_username("owner", "MathRex")
            room = main.rooms[code]
            room["round"]["start"] = time.monotonic() - 1
            question = room["round"]["questions"][0]
            await main.handle_message(guest, {"type": "answer", "room_code": code, "round_id": room["round"]["id"], "index": 0, "question_id": question["question_id"], "answer": question["answer"]})
            self.assertEqual(lookup.call_count, 2)
            with patch.object(main.outbox, "enqueue"):
                await main.finish_round(room)
            result = None
            while not guest["websocket"].outgoing.empty():
                result = await guest["websocket"].outgoing.get()
            self.assertEqual(result["display_names"]["MathRex"], "Guest · MathRex")
            await main.handle_message(guest, {"type": "play_again", "room_code": code})
            with self.assertRaisesRegex(ValueError, "belongs to a registered player"):
                await main.handle_message(guest, {"type": "start_game", "room_code": code, "duration": 60})

    async def test_refresh_and_logout_revalidate_guest_identity(self):
        guest = self.player(uid="unregistered")
        await self.enter(guest, "MathRex")
        self.firebase.register_username("owner", "MathRex")
        with self.assertRaisesRegex(ValueError, "belongs to a registered player"):
            await main.handle_message(guest, {"type": "refresh_profile"})
        with self.assertRaisesRegex(ValueError, "belongs to a registered player"):
            await main.authenticate(guest, None)
        self.assertIsNone(guest["identity"])
        self.assertEqual(main.display_name(guest), "Guest · MathRex")

    async def test_registration_in_another_session_becomes_canonical_at_entry_and_start(self):
        account = self.player(uid="owner")
        self.firebase.register_username("owner", "MathRex")
        code = await self.enter(account, "ClientChosen")
        self.assertEqual(main.player_name(account), "MathRex")
        other = self.player(uid="other")
        await self.enter(other, "Unregistered", code)
        self.firebase.register_username("other", "Second_Name")
        await main.handle_message(account, {"type": "start_game", "room_code": code, "duration": 60})
        participant = main.rooms[code]["round"]["participants"][other["id"]]
        self.assertEqual(participant["nickname"], "Second_Name")
        self.assertTrue(participant["eligible"])

    async def test_logout_cannot_create_duplicate_guest_display_names(self):
        self.firebase.register_username("owner", "Owner")
        owner = self.player("Owner", "owner")
        code = await self.enter(owner, "ignored")
        # An owner can retain an earlier raw nickname after a profile refresh.
        owner["nickname"] = "Available"
        await self.enter(self.player(), "Available", code)
        with self.assertRaisesRegex(ValueError, "already in use"):
            await main.authenticate(owner, None)
        self.assertIsNone(owner["room"])
        self.assertEqual(len(main.rooms[code]["players"]), 1)

    async def test_membership_change_during_start_validation_cancels_start(self):
        host, guest = self.player(), self.player()
        code = await self.enter(host, "Host")
        await self.enter(guest, "Other", code)
        original = main.validate_guest_nickname
        async def check_and_depart(player):
            await original(player)
            if player is host:
                await main.depart(guest)
        with patch.object(main, "validate_guest_nickname", side_effect=check_and_depart):
            with self.assertRaisesRegex(ValueError, "Room changed during identity verification"):
                await main.handle_message(host, {"type": "start_game", "room_code": code, "duration": 60})
        self.assertEqual(main.rooms[code]["state"], "lobby")
        self.assertIsNone(main.rooms[code]["round"])
