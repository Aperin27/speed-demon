import asyncio
import json
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException, WebSocketDisconnect
from backend import main


class Client:
    def __init__(self, origin="http://localhost:5500"):
        self.headers = {"origin": origin}
        self.scope = {"client": ("127.0.0.1", 12345), "headers": []}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = None

    async def accept(self):
        pass

    async def close(self, code, reason=None):
        self.closed = code

    async def receive(self):
        return {"type": "websocket.receive", "text": await self.receive_text()}

    async def receive_text(self):
        message = await self.incoming.get()
        if message is None:
            raise WebSocketDisconnect()
        return message if isinstance(message, str) else json.dumps(message)

    async def send_text(self, message):
        await self.outgoing.put(json.loads(message))

    async def send(self, message):
        await self.incoming.put(message)


class RoundFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.rooms.clear()
        main.security.limiter.clear()
        self.reservations = patch.object(main.firebase, "nickname_reserved", return_value=False)
        self.lookup = self.reservations.start()
        self.profiles = patch.object(main.firebase, "get_username", side_effect=lambda uid: next((p["identity"].get("username") for room in main.rooms.values() for p in room["players"] if p["identity"] and p["identity"]["uid"] == uid), None))
        self.profiles.start()
        self.saved = []
        self.enqueue = patch.object(main.outbox, "enqueue", side_effect=lambda record: self.saved.append(record))
        self.enqueue.start()
        self.host, self.guest = Client(), Client()
        self.tasks = []
        for client in (self.host, self.guest):
            self.tasks.append(asyncio.create_task(main.websocket_endpoint(client)))
        await self.host.send({"type": "create_room", "nickname": "Host"})
        self.host_id = (await self.receive(self.host, "joined"))["player_id"]
        created = await self.receive(self.host, "room_created")
        self.code = created["room_code"]
        await self.guest.send({"type": "join_room", "room_code": self.code, "nickname": "Guest"})
        self.guest_id = (await self.receive(self.guest, "joined"))["player_id"]
        await self.broadcast("player_joined")
        self.room = main.rooms[self.code]
        self.deadlines = []

    async def asyncTearDown(self):
        for task in self.tasks + self.deadlines:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.deadlines, return_exceptions=True)
        self.enqueue.stop()
        self.reservations.stop()
        self.profiles.stop()
        main.rooms.clear()

    async def receive(self, client, message_type):
        result = await asyncio.wait_for(client.outgoing.get(), 2)
        self.assertEqual(result["type"], message_type, result)
        return result

    async def broadcast(self, message_type):
        host_message = await self.receive(self.host, message_type)
        guest_message = await self.receive(self.guest, message_type)
        return host_message, guest_message

    async def start(self, duration=60):
        await self.host.send({"type": "start_game", "room_code": self.code, "duration": duration})
        messages = await self.broadcast("game_started")
        self.deadlines.append(self.room["task"])
        self.assertEqual(messages[0]["questions"], messages[1]["questions"])
        self.assertTrue(all("answer" not in q for q in messages[0]["questions"]))
        self.room["round"]["start"] = time.monotonic() - 1
        return messages

    def answer(self, player_id, correct=True):
        state = self.room["round"]
        index = state["participants"][player_id]["next"]
        question = state["questions"][index]
        return {"type": "answer", "room_code": self.code, "round_id": state["id"],
                "index": index, "question_id": question["question_id"],
                "answer": question["answer"] if correct else 999}

    async def finish(self):
        self.room["round"]["end"] = time.monotonic() - 1
        await main.finish_round(self.room)
        return (await self.broadcast("game_results"))[0]

    async def login(self, client, uid, registered=True):
        identity = {"uid": uid, "username": ("Host" if client is self.host else "Guest") if registered else None, "exp": time.time() + 3600}
        with patch.object(main.firebase, "verify_token", return_value=identity):
            await client.send({"type": "authenticate", "id_token": "opaque-token"})
            status = await self.receive(client, "auth_status")
            for member in self.room["players"]:
                await self.receive(member["websocket"], "identity_changed")
            return status

    async def test_both_answer_orders_replay_second_round_and_ties(self):
        for order in ((self.host, self.host_id, self.guest, self.guest_id),
                      (self.guest, self.guest_id, self.host, self.host_id)):
            await self.start()
            for client, player_id in ((order[0], order[1]), (order[2], order[3])):
                await client.send(self.answer(player_id))
                acknowledgement = await self.receive(client, "answer_result")
                self.assertEqual(acknowledgement["score"], 1)
                self.assertNotIn("answer", acknowledgement["questions"][0])
            result = await self.finish()
            self.assertEqual(result["scores"], {"Host": 1, "Guest": 1})
            self.assertEqual(result["winners"], ["Guest", "Host"])
            self.assertEqual(self.saved[-1]["results"], [])
            await self.host.send({"type": "play_again", "room_code": self.code})
            await self.broadcast("returned_to_lobby")
            self.assertIsNone(self.room["round"])
        self.assertNotEqual(self.saved[0]["round_id"], self.saved[1]["round_id"])

    async def test_forged_scores_rejected_in_both_orders(self):
        await self.start()
        for clients in ((self.host, self.guest), (self.guest, self.host)):
            for client in clients:
                await client.send({"type": "submit_score", "room_code": self.code, "nickname": "Host", "score": 999999})
                await self.receive(client, "error")
        self.assertEqual((await self.finish())["scores"], {"Host": 0, "Guest": 0})

    async def test_duplicate_stale_out_of_order_invalid_and_late_answers(self):
        await self.start()
        good = self.answer(self.host_id)
        for invalid in ({**good, "round_id": "old"}, {**good, "index": 1},
                        {**good, "question_id": "fake"}, {**good, "answer": True},
                        {**good, "answer": "2"}, {**good, "answer": float("nan")},
                        {**good, "answer": 10 ** 400}):
            await self.host.send(invalid)
            await self.receive(self.host, "error")
        await self.host.send(good)
        await self.receive(self.host, "answer_result")
        await self.host.send(good)
        await self.receive(self.host, "error")
        self.room["round"]["end"] = time.monotonic() - 1
        await self.host.send(self.answer(self.host_id))
        await self.receive(self.host, "error")
        self.assertEqual(self.room["round"]["participants"][self.host_id]["score"], 1)

    async def test_countdown_answers_rejected_wrong_answer_consumes_question(self):
        await self.start()
        self.room["round"]["start"] = time.monotonic() + 3
        await self.host.send(self.answer(self.host_id))
        await self.receive(self.host, "error")
        self.room["round"]["start"] = time.monotonic() - 1
        await self.host.send(self.answer(self.host_id, False))
        response = await self.receive(self.host, "answer_result")
        self.assertFalse(response["correct"])
        self.assertEqual(response["score"], -1)
        self.assertEqual(self.room["round"]["participants"][self.host_id]["next"], 1)

    async def test_host_membership_duration_repeated_start_and_active_replay(self):
        for message in ({"type": "start_game", "room_code": self.code, "duration": 61},
                        {"type": "start_game", "room_code": "OTHER", "duration": 60}):
            await self.host.send(message)
            await self.receive(self.host, "error")
        for kind in ("start_game", "play_again"):
            await self.guest.send({"type": kind, "room_code": self.code, "duration": 60})
            await self.receive(self.guest, "error")
        await self.start()
        original = self.room["round"]["id"]
        for kind in ("start_game", "play_again"):
            await self.host.send({"type": kind, "room_code": self.code, "duration": 60})
            await self.receive(self.host, "error")
        self.assertEqual(self.room["round"]["id"], original)

    async def test_auth_snapshot_late_login_and_signout(self):
        await self.login(self.host, "uid-a")
        started = await self.start()
        self.assertTrue(started[0]["ranked"])
        self.assertFalse(started[1]["ranked"])
        status = await self.login(self.guest, "uid-b")
        self.assertFalse(status["ranked"])
        await self.finish()
        self.assertEqual([p["uid"] for p in self.saved[-1]["results"]], ["uid-a"])
        await self.host.send({"type": "play_again", "room_code": self.code})
        await self.broadcast("returned_to_lobby")
        await self.start()
        await self.host.send({"type": "authenticate", "id_token": None})
        await self.receive(self.host, "auth_status")
        await self.broadcast("identity_changed")
        await self.finish()
        self.assertEqual([p["uid"] for p in self.saved[-1]["results"]], ["uid-b"])

    async def test_duplicate_account_and_expired_identity(self):
        await self.login(self.host, "same-uid")
        with patch.object(main.firebase, "verify_token", return_value={"uid": "same-uid", "name": "Same", "exp": time.time() + 3600}):
            await self.guest.send({"type": "authenticate", "id_token": "other-token"})
            response = await self.receive(self.guest, "auth_status")
            self.assertFalse(response["authenticated"])
            await self.broadcast("identity_changed")
            await self.receive(self.guest, "error")
        self.room["players"][0]["identity"]["exp"] = time.time() - 1
        started = await self.start()
        self.assertFalse(started[0]["ranked"])

    async def test_invalid_expired_tokens_fail_closed(self):
        for failure in (ValueError("invalid"), ValueError("expired")):
            self.room["players"][0]["last_auth"] = -float("inf")
            with patch.object(main.firebase, "verify_token", side_effect=failure):
                await self.host.send({"type": "authenticate", "id_token": "bad-token", "uid": "forged"})
                response = await self.receive(self.host, "auth_status")
                await self.broadcast("identity_changed")
                self.assertFalse(response["authenticated"])
                self.assertIsNone(self.room["players"][0]["identity"])

    async def test_disconnect_transfers_host_preserves_scores_and_disqualifies(self):
        await self.login(self.host, "host-uid")
        await self.start()
        await self.host.send(self.answer(self.host_id))
        await self.receive(self.host, "answer_result")
        await self.host.incoming.put(None)
        message = await self.receive(self.guest, "player_left")
        self.assertEqual(message["host"], self.guest_id)
        self.room["round"]["end"] = time.monotonic() - 1
        await main.finish_round(self.room)
        result = await self.receive(self.guest, "game_results")
        self.assertEqual(result["scores"]["Host"], 1)
        self.assertEqual(self.saved[-1]["results"], [])
        await self.guest.send({"type": "play_again", "room_code": self.code})
        await self.receive(self.guest, "returned_to_lobby")

    async def test_real_deadline_finishes_without_client_score_messages(self):
        await self.start()
        self.room["task"].cancel()
        await asyncio.gather(self.room["task"], return_exceptions=True)
        self.room["round"]["end"] = time.monotonic() + .01
        task = asyncio.create_task(main.round_deadline(self.room))
        self.deadlines.append(task)
        await self.broadcast("game_results")
        await main.finish_round(self.room)
        self.assertEqual(len(self.saved), 1)

    async def test_duplicate_nickname_midround_join_and_malformed_json(self):
        third = Client()
        self.tasks.append(asyncio.create_task(main.websocket_endpoint(third)))
        await third.send({"type": "join_room", "room_code": self.code, "nickname": "host"})
        await self.receive(third, "error")
        await self.start()
        await third.send({"type": "join_room", "room_code": self.code, "nickname": "Third"})
        await self.receive(third, "error")
        for raw in ("{", "[]", "null"):
            await third.send(raw)
            await self.receive(third, "error")

    async def test_origin_checks(self):
        for origin in (None, "https://attacker.example"):
            client = Client(origin)
            await main.websocket_endpoint(client)
            self.assertEqual(client.closed, 1008)

    async def test_malformed_replacement_token_revokes_ranked_eligibility(self):
        await self.login(self.host, "verified-uid")
        await self.start()
        await self.host.send({"type": "authenticate", "id_token": {"uid": "forged"}})
        response = await self.receive(self.host, "auth_status")
        self.assertFalse(response["authenticated"])
        self.assertFalse(response["ranked"])
        await self.broadcast("identity_changed")
        await self.receive(self.host, "error")
        await self.finish()
        self.assertEqual(self.saved[-1]["results"], [])

    async def test_enqueue_failure_is_visible_without_blocking_scores(self):
        await self.start()
        with patch.object(main.outbox, "enqueue", side_effect=OSError("disk full")):
            result = await self.finish()
        self.assertEqual(result["persistence"], "failed")
        self.assertEqual(result["scores"], {"Host": 0, "Guest": 0})

    async def test_unrelated_socket_cannot_act_in_a_known_room(self):
        stranger = Client()
        self.tasks.append(asyncio.create_task(main.websocket_endpoint(stranger)))
        for kind in ("start_game", "play_again", "answer", "submit_score"):
            await stranger.send({"type": kind, "room_code": self.code, "duration": 60, "nickname": "Host", "score": 999})
            await self.receive(stranger, "error")
        self.assertEqual(self.room["state"], "lobby")

    async def test_departed_uid_cannot_reappear_as_another_ranked_participant(self):
        await self.login(self.host, "original-uid")
        await self.start()
        await self.host.incoming.put(None)
        await self.receive(self.guest, "player_left")
        status = await self.login(self.guest, "different-uid")
        self.assertFalse(status["ranked"])
        self.room["players"][0]["last_auth"] = -float("inf")
        with patch.object(main.firebase, "verify_token", return_value={"uid": "original-uid", "name": "Original", "exp": time.time() + 3600}):
            await self.guest.send({"type": "authenticate", "id_token": "other-token"})
            status = await self.receive(self.guest, "auth_status")
            self.assertFalse(status["authenticated"])
            await self.receive(self.guest, "identity_changed")
            await self.receive(self.guest, "error")

    async def test_answer_at_exact_start_allowed_and_exact_end_rejected(self):
        await self.start()
        state = self.room["round"]
        player = self.room["players"][0]
        clock = type("Clock", (), {"monotonic": staticmethod(lambda: state["start"])})
        with patch.object(main, "time", clock):
            await main.handle_message(player, self.answer(self.host_id))
        await self.receive(self.host, "answer_result")
        clock.monotonic = staticmethod(lambda: state["end"])
        with patch.object(main, "time", clock), self.assertRaises(ValueError):
            await main.handle_message(player, self.answer(self.host_id))

    async def test_640_questions_for_every_duration_with_simulated_time(self):
        for duration in main.DURATIONS:
            with self.subTest(duration=duration):
                started = (await self.start(duration))[0]
                state = self.room["round"]
                participant = state["participants"][self.host_id]
                member = next(player for player in self.room["players"] if player["id"] == self.host_id)
                issued = {prompt["index"]: prompt for prompt in started["questions"]}
                self.assertEqual(len(issued), 32)
                # Replace only the backend's clock reference, leaving asyncio's clock real.
                clock = unittest.mock.Mock(wraps=time)
                with patch.object(main, "time", clock):
                    for index in range(640):
                        clock.monotonic.return_value = state["start"] + duration * (index + 1) / 641
                        prompt = issued.pop(index)
                        self.assertEqual(prompt["question_id"], state["questions"][index]["question_id"])
                        self.assertEqual(set(prompt), {"index", "question_id", "question"})
                        answer = self.answer(self.host_id, correct=index % 4 != 0)
                        await main.handle_message(member, answer)
                        acknowledgement = await self.receive(self.host, "answer_result")
                        self.assertEqual(acknowledgement["index"], index)
                        self.assertEqual(len(acknowledgement["questions"]), 1)
                        tail = acknowledgement["questions"][0]
                        self.assertEqual(tail["index"], index + 32)
                        self.assertEqual(set(tail), {"index", "question_id", "question"})
                        issued[tail["index"]] = tail
                        self.assertEqual(len(issued), 32)
                        self.assertEqual(participant["issued"] - participant["next"], 32)
                        if index % 32 == 0:
                            with self.assertRaisesRegex(ValueError, "Duplicate"):
                                await main.handle_message(member, answer)
                        self.assertEqual(participant["score"], index + 1 - 2 * (index // 4 + 1))
                        self.assertLess(len(state["questions"]) - participant["issued"], 32)
                    clock.monotonic.return_value = state["end"]
                    with self.assertRaisesRegex(ValueError, "outside round time"):
                        await main.handle_message(member, self.answer(self.host_id))
                self.assertEqual(participant["next"], 640)
                self.assertEqual(participant["score"], 320)
                self.assertEqual(len({q["question_id"] for q in state["questions"]}), len(state["questions"]))
                await self.finish()
                await self.host.send({"type": "play_again", "room_code": self.code})
                await self.broadcast("returned_to_lobby")

    async def test_all_durations_and_buffer_replenishment(self):
        for duration in main.DURATIONS:
            await self.start(duration)
            for _ in range(35):
                await self.host.send(self.answer(self.host_id))
                await self.receive(self.host, "answer_result")
            await self.finish()
            self.assertEqual(self.saved[-1]["duration"], duration)
            self.assertEqual(self.room["round"]["participants"][self.host_id]["score"], 35)
            await self.host.send({"type": "play_again", "room_code": self.code})
            await self.broadcast("returned_to_lobby")
            await asyncio.sleep(1.01)  # Don't trip the intentional connection rate limit.

    async def test_username_registration_during_round_only_ranks_next_round(self):
        status = await self.login(self.host, "uid-a", registered=False)
        self.assertEqual(status["eligibility"], "username_required")
        started = await self.start()
        self.assertFalse(started[0]["ranked"])
        with patch.object(main.firebase, "get_username", return_value="Host"):
            await self.host.send({"type": "refresh_profile", "uid": "forged"})
            status = await self.receive(self.host, "auth_status")
            await self.broadcast("identity_changed")
        self.assertEqual(status["username"], "Host")
        self.assertFalse(status["ranked"])
        await self.finish()
        self.assertEqual(self.saved[-1]["results"], [])
        await self.host.send({"type": "play_again", "room_code": self.code})
        await self.broadcast("returned_to_lobby")
        self.assertTrue((await self.start())[0]["ranked"])

    async def test_negative_scores_duplicates_and_negative_result_winner(self):
        await self.start()
        wrong = self.answer(self.host_id, False)
        await self.host.send(wrong)
        self.assertEqual((await self.receive(self.host, "answer_result"))["score"], -1)
        await self.host.send(wrong)
        await self.receive(self.host, "error")
        self.assertEqual(self.room["round"]["participants"][self.host_id]["score"], -1)
        for _ in range(2):
            await self.guest.send(self.answer(self.guest_id, False))
            await self.receive(self.guest, "answer_result")
        result = await self.finish()
        self.assertEqual(result["scores"], {"Host": -1, "Guest": -2})
        self.assertEqual(result["winners"], ["Host"])

    async def test_registered_name_overrides_client_nickname_without_private_fields(self):
        await self.login(self.host, "uid-a")
        self.room["players"][0]["identity"]["username"] = "Registered_Name"
        message = main.lobby_message(self.room, "identity_changed")
        self.assertEqual(message["players"][0]["nickname"], "Registered_Name")
        self.assertNotIn("uid", json.dumps(message))
        started = await self.start()
        self.assertTrue(started[0]["ranked"])
        result = await self.finish()
        self.assertIn("Registered_Name", result["scores"])
        self.assertEqual(self.saved[-1]["results"][0]["name"], "Registered_Name")

    async def test_unregistered_departed_uid_cannot_take_another_player_position(self):
        await self.login(self.host, "unregistered-uid", registered=False)
        await self.start()
        await self.host.incoming.put(None)
        await self.receive(self.guest, "player_left")
        status = await self.login(self.guest, "unregistered-uid", registered=False)
        self.assertFalse(status["authenticated"])
        await self.receive(self.guest, "error")


class LeaderboardEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.leaderboard_cache.clear()

    async def test_cache_query_duration_and_no_private_fields(self):
        with patch.object(main.firebase, "leaderboard", return_value=[{"name": "Player", "score": 4}]) as query:
            first = await main.leaderboard(60, "weekly")
            self.assertEqual(first, await main.leaderboard(60, "weekly"))
            self.assertEqual(query.call_count, 1)
            await main.leaderboard(120, "weekly")
            await main.leaderboard(60, "all_time")
            self.assertEqual(query.call_count, 3)
            self.assertNotIn("uid", first["entries"][0])

    async def test_failure_cached_and_bad_selections_rejected(self):
        with patch.object(main.firebase, "leaderboard", side_effect=RuntimeError("offline")) as query:
            for _ in range(2):
                with self.assertRaises(HTTPException) as raised:
                    await main.leaderboard(60, "weekly")
                self.assertEqual(raised.exception.status_code, 503)
            self.assertEqual(query.call_count, 1)
        for duration, period in ((61, "weekly"), (60, "fake")):
            with self.assertRaises(HTTPException) as raised:
                await main.leaderboard(duration, period)
            self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
