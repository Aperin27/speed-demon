import asyncio
import json
import unittest

from fastapi import WebSocketDisconnect

from backend.main import rooms, websocket_endpoint


class Client:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def accept(self):
        pass

    async def receive_text(self):
        message = await self.incoming.get()
        if message is None:
            raise WebSocketDisconnect()
        return json.dumps(message)

    async def send_text(self, message):
        await self.outgoing.put(json.loads(message))

    async def send(self, message):
        await self.incoming.put(message)
        await asyncio.sleep(0)


class RoundFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        rooms.clear()
        self.host = Client()
        self.guest = Client()
        self.tasks = [asyncio.create_task(websocket_endpoint(client))
                      for client in (self.host, self.guest)]
        await self.host.send({"type": "create_room", "nickname": "Host"})
        created = await self.receive(self.host, "room_created")
        self.code = created["room_code"]
        await self.guest.send({"type": "join_room", "room_code": self.code,
                               "nickname": "Guest"})
        await self.broadcast("player_joined")

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        rooms.clear()

    async def receive(self, client, expected_type):
        message = await asyncio.wait_for(client.outgoing.get(), 1)
        self.assertEqual(message["type"], expected_type)
        return message

    async def broadcast(self, expected_type):
        host_message = await self.receive(self.host, expected_type)
        self.assertEqual(await self.receive(self.guest, expected_type), host_message)
        return host_message

    async def start_round(self):
        await self.host.send({"type": "start_game", "room_code": self.code,
                              "duration": 60})
        started = await self.broadcast("game_started")
        room = rooms[self.code]
        self.assertTrue(room["started"])
        self.assertEqual(room["duration"], 60)
        self.assertEqual(room["questions"], started["questions"])
        self.assertEqual(len(room["questions"]), 200)
        self.assertEqual(room["scores"], {})

    async def finish_round(self, guest_first):
        submissions = [(self.host, "Host", 3), (self.guest, "Guest", 5)]
        if guest_first:
            submissions.reverse()
        for index, (client, name, score) in enumerate(submissions):
            await client.send({"type": "submit_score", "room_code": self.code,
                               "nickname": name, "score": score})
            self.assertEqual(rooms[self.code]["scores"][name], score)
            self.assertTrue(rooms[self.code]["started"])
            if index == 0:
                self.assertEqual(rooms[self.code]["scores"], {name: score})
                self.assertTrue(self.host.outgoing.empty())
                self.assertTrue(self.guest.outgoing.empty())
        results = await self.broadcast("game_results")
        self.assertEqual(results["scores"], {"Host": 3, "Guest": 5})
        self.assertEqual(results["winner"], "Guest")

    async def replay(self):
        members = list(rooms[self.code]["players"])
        await self.host.send({"type": "play_again", "room_code": self.code})
        lobby = await self.broadcast("returned_to_lobby")
        self.assertEqual(lobby["players"], ["Host", "Guest"])
        self.assertEqual(lobby["host"], "Host")
        self.assertEqual(lobby["room_code"], self.code)
        room = rooms[self.code]
        self.assertFalse(room["started"])
        self.assertEqual(room["scores"], {})
        self.assertEqual(room["questions"], [])
        self.assertEqual(room["players"], members)

    async def test_host_first_then_guest_first_after_replay(self):
        await self.start_round()
        await self.finish_round(guest_first=False)
        await self.replay()
        await self.start_round()
        await self.finish_round(guest_first=True)

    async def test_guest_first_then_host_first_after_replay(self):
        await self.start_round()
        await self.finish_round(guest_first=True)
        await self.replay()
        await self.start_round()
        await self.finish_round(guest_first=False)

    async def test_guest_cannot_start_or_replay(self):
        await self.start_round()
        original_questions = rooms[self.code]["questions"]
        for message_type in ("start_game", "play_again"):
            await self.guest.send({"type": message_type, "room_code": self.code})
            await self.receive(self.guest, "error")
            self.assertTrue(rooms[self.code]["started"])
            self.assertIs(rooms[self.code]["questions"], original_questions)
            self.assertTrue(self.host.outgoing.empty())

    async def test_unknown_messages_do_not_reset_round(self):
        await self.start_round()
        original_questions = rooms[self.code]["questions"]
        for client in (self.host, self.guest):
            for message in ({"type": "unrelated"},
                            {"type": "play again", "room_code": self.code}):
                await client.send(message)
                self.assertTrue(rooms[self.code]["started"])
                self.assertIs(rooms[self.code]["questions"], original_questions)
                self.assertTrue(self.host.outgoing.empty())
                self.assertTrue(self.guest.outgoing.empty())

    async def test_missing_replay_room_does_not_reset_existing_room(self):
        await self.start_round()
        await self.host.send({"type": "play_again", "room_code": "missing"})
        error = await self.receive(self.host, "error")
        self.assertEqual(error["message"], "Room not found")
        self.assertTrue(rooms[self.code]["started"])

    async def test_duplicate_host_nickname_cannot_replay(self):
        duplicate = Client()
        self.tasks.append(asyncio.create_task(websocket_endpoint(duplicate)))
        await duplicate.send({"type": "join_room", "room_code": self.code,
                              "nickname": "Host"})
        await self.receive(self.host, "player_joined")
        await self.receive(self.guest, "player_joined")
        await self.receive(duplicate, "player_joined")
        await self.start_round()
        await self.receive(duplicate, "game_started")
        await duplicate.send({"type": "play_again", "room_code": self.code})
        error = await self.receive(duplicate, "error")
        self.assertEqual(error["message"], "Only the host can start a new round")
        self.assertTrue(rooms[self.code]["started"])
        self.assertTrue(self.host.outgoing.empty())


if __name__ == "__main__":
    unittest.main()
