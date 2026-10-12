import asyncio
import ipaddress
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from starlette.datastructures import Headers

from backend import main, security
from tests.local_server import LocalFirebase
from tests.test_round_flow import Client


class LimiterTests(unittest.TestCase):
    def test_enforcement_refill_and_isolated_identities(self):
        now = [0]
        limiter = security.RateLimiter({"TEST": (2, 10)}, clock=lambda: now[0])
        limiter.require(("TEST", "a"))
        limiter.require(("TEST", "a"))
        with self.assertRaises(security.RateLimitError) as error:
            limiter.require(("TEST", "a"))
        self.assertEqual(error.exception.retry_after, 5)
        limiter.require(("TEST", "b"))
        now[0] = 5
        limiter.require(("TEST", "a"))
        with self.assertRaises(security.RateLimitError):
            limiter.require(("TEST", "a"))
        now[0] = 15
        limiter.prune()
        self.assertEqual(limiter.buckets, {})

    def test_bounded_store_does_not_evict_live_counters_or_partially_charge(self):
        now = [0]
        limiter = security.RateLimiter({"TEST": (1, 30)}, max_keys=2, clock=lambda: now[0])
        limiter.require(("TEST", "a"))
        with self.assertRaises(security.RateLimitError):
            limiter.require(("TEST", "new"), ("TEST", "a"))
        self.assertNotIn(("TEST", "new"), limiter.buckets)
        limiter.require(("TEST", "b"))
        with self.assertRaises(security.RateLimitError):
            limiter.require(("TEST", "c"))
        self.assertEqual(len(limiter.buckets), 2)
        now[0] = 30
        limiter.require(("TEST", "c"))
        self.assertEqual(len(limiter.buckets), 1)

    def test_retry_after_accounts_for_every_exhausted_bucket(self):
        limiter = security.RateLimiter({"SHORT": (1, 5), "LONG": (1, 30)}, clock=lambda: 0)
        limiter.require(("SHORT", "a"), ("LONG", "a"))
        with self.assertRaises(security.RateLimitError) as error:
            limiter.require(("SHORT", "a"), ("LONG", "a"))
        self.assertEqual(error.exception.retry_after, 30)

    def test_forwarded_addresses_only_from_trusted_physical_peers(self):
        def scope(peer, forwarded, port=123):
            return {"client": (peer, port), "headers": [(b"x-forwarded-for", forwarded.encode())]}
        with patch.object(security, "TRUSTED_PROXY_NETWORKS", (ipaddress.ip_network("10.1.0.0/16"),)):
            self.assertEqual(security.client_key(scope("198.51.100.1", "203.0.113.2")), "198.51.100.1")
            self.assertEqual(security.client_key(scope("10.1.0.1", "192.0.2.9, 203.0.113.2, 10.1.0.2")), "203.0.113.2")
            self.assertEqual(security.client_key(scope("10.1.0.1", "invalid-address")), "10.1.0.1")
            duplicate = scope("10.1.0.1", "203.0.113.2")
            duplicate["headers"].append((b"x-forwarded-for", b"203.0.113.3"))
            self.assertEqual(security.client_key(duplicate), "10.1.0.1")
            self.assertEqual(security.client_key(scope("203.0.113.2", "192.0.2.9", 0)), "unverified-proxy")
            self.assertEqual(security.client_key(scope("::ffff:198.51.100.1", "203.0.113.2")), "198.51.100.1")

    def test_json_and_schema_reject_ambiguous_or_non_numeric_values(self):
        for raw in ('{"type":"answer","type":"authenticate"}', '{"answer":NaN}', '{"answer":Infinity}', '[' * 2000 + ']' * 2000):
            with self.subTest(raw=raw[:30]), self.assertRaises((ValueError, RecursionError)):
                security.decode_json(raw)
        for value in (None, [], {}, {"type": []}, {"type": "unknown"}, {"type": "join_room", "room_code": {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                security.validate_message(value)
        valid = {"type": "answer", "room_code": "ABC123", "round_id": "a" * 32, "question_id": "b" * 32, "index": 0, "answer": 4}
        for answer in (True, "4", None, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(answer=str(answer)[:20]), self.assertRaises(ValueError):
                security.validate_message({**valid, "answer": answer})
        security.validate_message(valid)

    def test_operational_logs_are_throttled_and_never_include_exception_contents(self):
        with patch.object(security, "_last_warning", {}), patch.object(security.logger, "log") as log:
            for _ in range(100):
                security.operational_failure("token_verification")
            self.assertEqual(log.call_count, 1)
            self.assertEqual(log.call_args.args[2], "token_verification")


async def request(path, method="GET", body=b"", ip="198.51.100.1", headers=(), chunks=None, query=""):
    events = asyncio.Queue()
    chunks = chunks if chunks is not None else [body]
    for index, chunk in enumerate(chunks):
        await events.put({"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1})
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "root_path": "", "query_string": query.encode(),
             "server": ("localhost", 8000), "client": (ip, 12345),
             "headers": [(b"content-type", b"application/json"), (b"origin", b"http://localhost:5500"), *headers]}
    responses = []
    async def send(event):
        responses.append(event)
    await main.app(scope, events.get, send)
    start = next(event for event in responses if event["type"] == "http.response.start")
    content = b"".join(event.get("body", b"") for event in responses if event["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), json.loads(content) if content else None


class HttpSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [0]
        self.limiter = security.RateLimiter(dict(security.POLICIES), clock=lambda: self.now[0])
        self.limiter_patch = patch.object(security, "limiter", self.limiter)
        self.limiter_patch.start()
        self.fake = LocalFirebase()
        self.firebase_patch = patch.object(main, "firebase", self.fake)
        self.firebase_patch.start()
        main.leaderboard_cache.clear()

    async def asyncTearDown(self):
        self.firebase_patch.stop()
        self.limiter_patch.stop()
        main.leaderboard_cache.clear()

    async def test_leaderboard_limit_recovery_cors_and_no_private_fields(self):
        self.limiter.policies["LEADERBOARD_IP"] = (2, 10)
        with patch.object(self.fake, "leaderboard", return_value=[{"name": "MathRex", "score": -1, "uid": "private", "email": "private@example.test"}]) as read:
            status, _, data = await request("/leaderboard")
            self.assertEqual(status, 200)
            self.assertEqual(data["entries"], [{"name": "MathRex", "score": -1}])
            await request("/leaderboard")
            status, headers, data = await request("/leaderboard", headers=[(b"x-forwarded-for", b"203.0.113.1")])
            self.assertEqual(status, 429)
            self.assertEqual(headers[b"retry-after"], b"5")
            self.assertEqual(headers[b"access-control-allow-origin"], b"http://localhost:5500")
            self.assertIn("Try again", data["detail"])
            self.assertEqual(read.call_count, 1)
            self.now[0] = 5
            self.assertEqual((await request("/leaderboard"))[0], 200)
            self.assertEqual((await request("/leaderboard", ip="198.51.100.2"))[0], 200)

    async def test_http_global_and_profile_limits_apply_before_verification(self):
        self.limiter.policies["PROFILE_IP"] = (1, 10)
        with patch.object(self.fake, "verify_token", wraps=self.fake.verify_token) as verify:
            headers = [(b"authorization", b"Bearer local-smoke-account-a")]
            self.assertEqual((await request("/profile", headers=headers))[0], 200)
            self.assertEqual((await request("/profile", headers=headers))[0], 429)
            self.assertEqual(verify.call_count, 1)
        self.limiter.clear()
        self.limiter.policies["HTTP_GLOBAL"] = (2, 10)
        for ip in ("198.51.100.1", "198.51.100.2"):
            self.assertEqual((await request("/", ip=ip))[0], 200)
        self.assertEqual((await request("/", ip="198.51.100.3"))[0], 429)

    async def test_registration_uid_limit_and_idempotent_retry_avoid_transactions(self):
        headers = [(b"authorization", b"Bearer local-smoke-account-a")]
        self.limiter.policies["REGISTER_UID"] = (2, 10)
        with patch.object(self.fake, "register_username", wraps=self.fake.register_username) as register:
            status, _, data = await request("/username", "POST", b'{"username":"MathRex","uid":"forged"}', headers=headers)
            self.assertEqual(status, 200)
            self.assertNotIn("uid", data)
            self.assertEqual(self.fake.profiles, {"local-smoke-account-a": "MathRex"})
            self.assertEqual((await request("/username", "POST", b'{"username":"MATHREX"}', headers=headers))[0], 200)
            self.assertEqual(register.call_count, 1)
            status, _, _ = await request("/username", "POST", b'{"username":"MathRex"}', ip="198.51.100.2", headers=headers)
            self.assertEqual(status, 429)
            self.now[0] = 5
            self.assertEqual((await request("/username", "POST", b'{"username":"OtherName"}', headers=headers))[0], 409)
            self.assertEqual(register.call_count, 1)

    async def test_registration_ip_limit_and_bad_names_prevent_firebase_work(self):
        self.limiter.policies["REGISTER_IP"] = (1, 10)
        with patch.object(self.fake, "verify_token", wraps=self.fake.verify_token) as verify, patch.object(self.fake, "register_username", wraps=self.fake.register_username) as register:
            self.assertEqual((await request("/username", "POST", b'{"username":"bad name"}'))[0], 400)
            self.assertEqual((await request("/username", "POST", b'{"username":"MathRex"}'))[0], 429)
            self.assertEqual(verify.call_count, 0)
            self.assertEqual(register.call_count, 0)

    async def test_invalid_json_oversize_chunks_and_validation_are_sanitized(self):
        with patch.object(self.fake, "verify_token") as verify:
            for body in (b'{"username":"PrivateToken",', b'{"username":NaN}', b'{"username":"a","username":"b"}', b'[]', b'[' * 1500 + b']' * 1500):
                self.assertEqual((await request("/username", "POST", body))[0], 400)
            status, _, _ = await request("/username", "POST", chunks=[b"x" * 3000, b"x" * 3000], headers=[(b"content-length", b"1")])
            self.assertEqual(status, 413)
            self.assertEqual((await request("/username", "POST", headers=[(b"content-length", b"99999")]))[0], 413)
            status, _, data = await request("/leaderboard", query="duration=PrivateToken")
            self.assertEqual(status, 422)
            self.assertNotIn("PrivateToken", json.dumps(data))
            self.assertEqual(verify.call_count, 0)

    async def test_slow_body_times_out_without_firebase_work(self):
        scope = {"type": "http", "method": "POST", "path": "/username", "headers": [], "client": ("198.51.100.1", 12)}
        responses = []
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            responses.append(message)
        with patch.object(security, "HTTP_BODY_SECONDS", 0.01):
            await security.SecurityMiddleware(AsyncMock())(scope, receive, send)
        self.assertEqual(responses[0]["status"], 408)


class SecurityClient(Client):
    def __init__(self, ip="198.51.100.1", origin="http://localhost:5500"):
        super().__init__(origin)
        self.scope = {"client": (ip, 12345), "headers": []}
        self.accepted = asyncio.Event()

    async def accept(self):
        self.accepted.set()

    async def close(self, code, reason=None):
        self.closed = code
        await self.incoming.put(None)

    async def receive(self):
        value = await self.incoming.get()
        if value is None:
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "websocket.receive", "bytes" if isinstance(value, bytes) else "text": value if isinstance(value, (str, bytes)) else json.dumps(value)}


class SocketSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.rooms.clear()
        main.connections.clear()
        main.connection_ips.clear()
        self.now = [0]
        self.limiter = security.RateLimiter(dict(security.POLICIES), clock=lambda: self.now[0])
        self.limiter_patch = patch.object(security, "limiter", self.limiter)
        self.limiter_patch.start()
        self.fake = LocalFirebase()
        self.firebase_patch = patch.object(main, "firebase", self.fake)
        self.firebase_patch.start()
        self.tasks = []
        self.deadlines = []

    async def asyncTearDown(self):
        for task in [*self.tasks, *self.deadlines]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.deadlines, return_exceptions=True)
        self.firebase_patch.stop()
        self.limiter_patch.stop()
        main.rooms.clear()
        self.assertEqual(main.connections, set())
        self.assertEqual(main.connection_ips, {})

    async def connect(self, client=None):
        client = client or SecurityClient()
        self.tasks.append(asyncio.create_task(main.websocket_endpoint(client)))
        await asyncio.wait_for(client.accepted.wait(), 2)
        return client

    async def receive(self, client, kind):
        value = await asyncio.wait_for(client.outgoing.get(), 2)
        self.assertEqual(value["type"], kind, value)
        return value

    async def create(self, client, name="Host"):
        await client.send({"type": "create_room", "nickname": name})
        player_id = (await self.receive(client, "joined"))["player_id"]
        code = (await self.receive(client, "room_created"))["room_code"]
        return player_id, code

    async def test_concurrent_handshake_reserves_capacity_before_accept(self):
        first = SecurityClient()
        entered, unblock = asyncio.Event(), asyncio.Event()
        async def accept():
            entered.set()
            await unblock.wait()
        with patch.object(security, "MAX_CONNECTIONS", 1), patch.object(first, "accept", side_effect=accept):
            self.tasks.append(asyncio.create_task(main.websocket_endpoint(first)))
            await entered.wait()
            other = SecurityClient("198.51.100.2")
            await main.websocket_endpoint(other)
            self.assertEqual(other.closed, 1008)
            self.assertEqual(len(main.connections), 1)
            self.tasks[0].cancel()
            await asyncio.gather(self.tasks[0], return_exceptions=True)
        self.assertEqual(len(main.connections), 0)

    async def test_per_ip_connection_cap_and_handshake_failure_cleanup(self):
        with patch.object(security, "MAX_CONNECTIONS_PER_IP", 2):
            await self.connect()
            await self.connect()
            rejected = SecurityClient()
            await main.websocket_endpoint(rejected)
            self.assertEqual(rejected.closed, 1008)
            await self.connect(SecurityClient("198.51.100.2"))
        failing = SecurityClient("198.51.100.3")
        with patch.object(failing, "accept", side_effect=RuntimeError("handshake failed")):
            await main.websocket_endpoint(failing)
        self.assertNotIn("198.51.100.3", main.connection_ips.values())

    async def test_connection_attempt_limit_recovery_and_exact_origin(self):
        self.limiter.policies["CONNECT_IP"] = (2, 10)
        for origin in ("null", "http://localhost:5500.attacker.test"):
            rejected = SecurityClient(origin=origin)
            await main.websocket_endpoint(rejected)
            self.assertEqual(rejected.closed, 1008)
        rejected = SecurityClient()
        await main.websocket_endpoint(rejected)
        self.assertEqual(rejected.closed, 1008)
        self.now[0] = 5
        await self.connect()

    async def test_duplicate_and_missing_origins_are_rejected(self):
        for headers in (Headers(), Headers(raw=[(b"origin", b"http://localhost:5500"), (b"origin", b"http://localhost:5500")])):
            client = SecurityClient()
            client.headers = headers
            await main.websocket_endpoint(client)
            self.assertEqual(client.closed, 1008)
            self.assertFalse(client.accepted.is_set())

    async def test_handshake_throttle_returns_http_retry_when_supported(self):
        self.limiter.policies["CONNECT_IP"] = (1, 10)
        self.limiter.require(("CONNECT_IP", "198.51.100.1"))
        client = SecurityClient()
        client.scope["extensions"] = {"websocket.http.response": {}}
        client.send_denial_response = AsyncMock()
        await main.websocket_endpoint(client)
        response = client.send_denial_response.call_args.args[0]
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "10")

    async def test_identity_throttle_stops_firebase_and_revokes_old_identity(self):
        client = await self.connect()
        _, code = await self.create(client)
        self.limiter.policies["IDENTITY_SOCKET"] = (1, 60)
        with patch.object(self.fake, "verify_token", wraps=self.fake.verify_token) as verify:
            await client.send({"type": "authenticate", "id_token": "local-smoke-account-a"})
            self.assertTrue((await self.receive(client, "auth_status"))["authenticated"])
            await self.receive(client, "identity_changed")
            await client.send({"type": "authenticate", "id_token": "local-smoke-account-a"})
            self.assertFalse((await self.receive(client, "auth_status"))["authenticated"])
            await self.receive(client, "identity_changed")
            self.assertIn("Too many requests", (await self.receive(client, "error"))["message"])
            self.assertEqual(verify.call_count, 1)
        self.assertIsNone(main.rooms[code]["players"][0]["identity"])

    async def test_room_creation_throttle_stops_reservation_reads_and_recovers(self):
        self.limiter.policies["CREATE_SOCKET"] = (2, 10)
        client = await self.connect()
        with patch.object(self.fake, "nickname_reserved", wraps=self.fake.nickname_reserved) as lookup:
            for _ in range(2):
                _, code = await self.create(client)
                await client.send({"type": "leave_room", "room_code": code})
            await client.send({"type": "create_room", "nickname": "Host"})
            response = await self.receive(client, "error")
            self.assertEqual(response["retry_after"], 5)
            self.assertEqual(lookup.call_count, 2)
            self.now[0] = 5
            await self.create(client)
            self.assertEqual(lookup.call_count, 3)

    async def test_join_attempts_limited_before_reservation_reads(self):
        self.limiter.policies["JOIN_SOCKET"] = (2, 10)
        client = await self.connect()
        with patch.object(self.fake, "nickname_reserved") as lookup:
            for _ in range(2):
                await client.send({"type": "join_room", "room_code": "ABC123", "nickname": "Available"})
                self.assertIn("Room not found", (await self.receive(client, "error"))["message"])
            await client.send({"type": "join_room", "room_code": "ABC123", "nickname": "Available"})
            self.assertIn("Too many requests", (await self.receive(client, "error"))["message"])
            lookup.assert_not_called()

    async def test_oversized_utf8_message_closes_and_cleans_room(self):
        client = await self.connect()
        _, code = await self.create(client)
        await client.send("é" * (security.WS_MESSAGE_BYTES // 2 + 1))
        self.assertIn("too large", (await self.receive(client, "error"))["message"])
        await asyncio.wait_for(self.tasks[-1], 2)
        self.assertEqual(client.closed, 1009)
        self.assertNotIn(code, main.rooms)

    async def test_json_binary_and_unauthorised_error_budget(self):
        client = await self.connect()
        self.limiter.policies["ERROR_SOCKET"] = (3, 30)
        for message in ('{"type":', b'{"type":"create_room"}', {"type": "start_game", "room_code": "ABC123", "duration": 60}):
            await client.send(message)
            await self.receive(client, "error")
        await client.send({"type": "refresh_profile"})
        self.assertIn("Too many invalid", (await self.receive(client, "error"))["message"])
        await asyncio.wait_for(self.tasks[-1], 2)
        self.assertEqual(client.closed, 1008)

    async def test_unused_room_expiration_and_active_round_exemption(self):
        client = await self.connect()
        _, code = await self.create(client)
        room = main.rooms[code]
        room["last_activity"] = time.monotonic() - security.ROOM_IDLE_SECONDS - 1
        room["state"] = "playing"
        await main.expire_rooms()
        self.assertIn(code, main.rooms)
        room["state"] = "results"
        await main.expire_rooms()
        self.assertIn("inactive room expired", (await self.receive(client, "error"))["message"])
        await asyncio.wait_for(asyncio.gather(self.tasks[-1], return_exceptions=True), 2)
        self.assertNotIn(code, main.rooms)
        self.assertEqual(client.closed, 1001)

    async def test_idle_and_unjoined_connection_timeouts(self):
        with patch.object(security, "UNJOINED_SECONDS", 0.01):
            client = await self.connect()
            self.assertIn("Idle connection expired", (await self.receive(client, "error"))["message"])
            await asyncio.wait_for(self.tasks[-1], 2)
            self.assertEqual(client.closed, 1001)

    async def test_leaving_a_room_gets_a_fresh_rejoin_window(self):
        client = await self.connect()
        _, code = await self.create(client)
        player = main.rooms[code]["players"][0]
        player["unjoined_since"] = time.monotonic() - security.UNJOINED_SECONDS - 1
        await client.send({"type": "leave_room", "room_code": code})
        await self.create(client, "Rejoined")
        self.assertIsNone(client.closed)

    async def test_invalid_text_encoding_is_rejected_without_crashing(self):
        client = await self.connect()
        await client.send("\ud800")
        self.assertIn("Invalid text encoding", (await self.receive(client, "error"))["message"])
        self.assertIsNone(client.closed)

    async def test_maintenance_is_stopped_even_if_lifespan_body_fails(self):
        entered = asyncio.Event()
        async def maintenance():
            entered.set()
            await asyncio.Event().wait()
        with patch.object(main.outbox, "start") as start, patch.object(main.outbox, "stop", new_callable=AsyncMock) as stop, patch.object(main, "maintain_rooms", side_effect=maintenance):
            with self.assertRaisesRegex(RuntimeError, "shutdown"):
                async with main.lifespan(main.app):
                    await entered.wait()
                    raise RuntimeError("shutdown")
            start.assert_called_once()
            stop.assert_awaited_once()

    async def test_room_capacity_limit_rejects_before_firebase(self):
        client = await self.connect()
        await self.create(client)
        other = await self.connect(SecurityClient("198.51.100.2"))
        with patch.object(security, "MAX_ROOMS", 1), patch.object(self.fake, "nickname_reserved") as lookup:
            await other.send({"type": "create_room", "nickname": "Other"})
            self.assertIn("Server is busy", (await self.receive(other, "error"))["message"])
            lookup.assert_not_called()

    async def test_fast_valid_answers_ignore_control_and_error_quotas(self):
        host, guest = await self.connect(), await self.connect()
        host_id, code = await self.create(host)
        await guest.send({"type": "join_room", "room_code": code, "nickname": "Guest"})
        await self.receive(guest, "joined")
        await self.receive(guest, "player_joined")
        await self.receive(host, "player_joined")
        await host.send({"type": "start_game", "room_code": code, "duration": 60})
        await self.receive(host, "game_started")
        await self.receive(guest, "game_started")
        room = main.rooms[code]
        self.deadlines.append(room["task"])
        room["round"]["start"] = time.monotonic() - 1
        self.limiter.policies["CONTROL_SOCKET"] = (1, 3600)
        self.limiter.policies["ERROR_SOCKET"] = (1, 3600)
        self.limiter.buckets[("CONTROL_SOCKET", host_id)] = (0, self.now[0])
        self.limiter.buckets[("ERROR_SOCKET", host_id)] = (0, self.now[0])
        with patch.object(self.fake, "nickname_reserved") as lookup:
            for index in range(256):
                question = room["round"]["questions"][index]
                await host.send({"type": "answer", "room_code": code, "round_id": room["round"]["id"], "question_id": question["question_id"], "index": index, "answer": question["answer"] if index % 2 == 0 else 999})
                response = await self.receive(host, "answer_result")
                self.assertEqual(response["score"], 1 if index % 2 == 0 else 0)
                self.assertEqual(room["round"]["participants"][host_id]["issued"] - room["round"]["participants"][host_id]["next"], 32)
            lookup.assert_not_called()
        self.assertEqual(host.closed, None)
        self.assertEqual(self.now[0], 0, "All answers were accepted in one simulated rate-limit instant")
