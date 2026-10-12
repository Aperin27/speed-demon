import asyncio
import json
import logging
import math
import os
import random
import secrets
import string
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError

from . import security
from .firebase_service import DURATIONS, SCORING_VERSION, UsernameError, firebase, normalize_nickname, validate_username, week_key
from .persistence import outbox

ALLOWED_ORIGINS = {origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "https://speed-demons.vercel.app,http://localhost:5500,http://127.0.0.1:5500").split(",")}
rooms = {}
connections = set()
connection_ips = {}
round_tasks = set()
leaderboard_cache = {}
leaderboard_lock = asyncio.Lock()
auth_slots = asyncio.Semaphore(4)


@asynccontextmanager
async def lifespan(app):
    outbox.start()
    maintenance = asyncio.create_task(maintain_rooms())
    try:
        yield
    finally:
        tasks = [maintenance, *round_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await outbox.stop()


app = FastAPI(lifespan=lifespan)
app.add_middleware(security.SecurityMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    return JSONResponse({"detail": "Invalid request parameters."}, status_code=422)


def claim_connection(player_id, ip):
    if len(connections) >= security.MAX_CONNECTIONS or sum(value == ip for value in connection_ips.values()) >= security.MAX_CONNECTIONS_PER_IP:
        return False
    connections.add(player_id)
    connection_ips[player_id] = ip
    return True


def release_connection(player_id):
    connections.discard(player_id)
    connection_ips.pop(player_id, None)


async def expire_rooms(now=None):
    now = time.monotonic() if now is None else now
    expired_players = []
    for room in list(rooms.values()):
        if room["state"] == "playing" or now - room.get("last_activity", now) < security.ROOM_IDLE_SECONDS:
            continue
        rooms.pop(room["code"], None)
        members = list(room["players"])
        for player in members:
            player["room"] = None
            player["closing"] = True
        expired_players.extend(members)
        room["players"].clear()
    # All expired slots are removed before any network await. Fan-out is bounded
    # by the existing connection cap, so one stalled client cannot delay others.
    await asyncio.gather(*(close_expired_player(player) for player in expired_players))


async def close_expired_player(player):
    await send(player, {"type": "error", "message": "This inactive room expired. Create or join another room.", "request_type": None})
    player["closed"] = True
    try:
        await asyncio.wait_for(player["websocket"].close(code=1001), 3)
    except Exception:
        pass
    task = player.get("task")
    if task and not task.done():
        task.cancel()


async def maintain_rooms():
    while True:
        await asyncio.sleep(security.MAINTENANCE_SECONDS)
        security.limiter.prune()
        await expire_rooms()


def generate_room_code():
    while True:
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        if code not in rooms:
            return code


def generate_questions(count=32):
    questions = []
    for _ in range(count):
        left, right = random.randint(1, 12), random.randint(1, 12)
        operator = random.choice(["+", "-", "×"])
        answer = left + right if operator == "+" else left - right if operator == "-" else left * right
        questions.append({"question_id": uuid.uuid4().hex, "question": f"{left} {operator} {right}", "answer": answer})
    return questions


def prompts(questions, start=0):
    return [{"question_id": question["question_id"], "question": question["question"], "index": start + index}
            for index, question in enumerate(questions)]


async def send(player, message):
    if player["closed"]:
        return
    try:
        async with player["send_lock"]:
            await asyncio.wait_for(player["websocket"].send_text(json.dumps(message)), 3)
    except Exception:
        security.operational_failure("socket_send")
        player["closed"] = True
        await depart(player)
        try:
            await player["websocket"].close(code=1011)
        except Exception:
            pass


def player_name(player):
    identity = player["identity"]
    return (identity.get("username") if identity else None) or player["nickname"]


def display_name(player):
    identity = player["identity"]
    return identity["username"] if identity and identity.get("username") else f"Guest · {player['nickname']}"


async def validate_guest_nickname(player):
    if player["identity"] and player["identity"].get("username"):
        return
    try:
        async with auth_slots:
            reserved = await asyncio.to_thread(firebase.nickname_reserved, player["nickname"])
    except Exception:
        security.operational_failure("nickname_reservation")
        raise ValueError("Nickname verification unavailable. Try again.") from None
    if reserved:
        raise ValueError("This nickname belongs to a registered player. Leave the room and choose another nickname.")


async def ensure_unique_identity(player, room):
    if any(other is not player and normalize_nickname(display_name(other)) == normalize_nickname(display_name(player)) for other in room["players"]):
        await depart(player)
        raise ValueError("That nickname is already in use. Choose another nickname before rejoining.")


async def refresh_registered_identity(player):
    identity = player["identity"]
    if not identity:
        return
    try:
        async with auth_slots:
            username = await asyncio.to_thread(firebase.get_username, identity["uid"])
    except Exception:
        security.operational_failure("profile_read")
        raise ValueError("Username lookup unavailable. Try again.") from None
    identity["username"] = username


def eligibility(player):
    identity = player["identity"]
    if not identity or identity["exp"] <= time.time():
        return "guest"
    return "ranked" if identity.get("username") else "username_required"


def lobby_message(room, message_type):
    return {"type": message_type, "room_code": room["code"], "host": room["host"],
            "players": [{"player_id": p["id"], "nickname": player_name(p), "display_name": display_name(p), "authenticated": p["identity"] is not None,
                         "registered": bool(p["identity"] and p["identity"].get("username")), "eligibility": eligibility(p)}
                        for p in room["players"]]}


async def broadcast(room, message):
    await asyncio.gather(*(send(player, message) for player in list(room["players"])))


async def depart(player):
    room = rooms.get(player["room"])
    player["room"] = None
    player["unjoined_since"] = time.monotonic()
    if not room or player not in room["players"]:
        return
    room["players"].remove(player)
    room["last_activity"] = time.monotonic()
    if room.get("round") and player["id"] in room["round"]["participants"]:
        room["round"]["participants"][player["id"]]["eligible"] = False
    if room["players"]:
        if room["host"] == player["id"]:
            room["host"] = room["players"][0]["id"]
        await broadcast(room, lobby_message(room, "player_left"))
    else:
        # The deadline task keeps the round alive long enough to record completion.
        rooms.pop(room["code"], None)


async def finish_round(room):
    round_state = room.get("round")
    if not round_state or room["state"] != "playing":
        return
    room["state"] = "results"
    room["last_activity"] = time.monotonic()
    participants = round_state["participants"]
    scores = {p["nickname"]: p["score"] for p in participants.values()}
    display_names = {p["nickname"]: p["display_name"] for p in participants.values()}
    highest = max(scores.values())
    winners = sorted(name for name, score in scores.items() if score == highest)
    record = {"round_id": round_state["id"], "scoring_version": SCORING_VERSION,
              "duration": room["duration"], "started_at": round_state["start_wall"],
              "completed_at": round_state["end_wall"], "week": week_key(round_state["start_wall"]),
              "player_count": len(participants), "results": [
                  {"uid": p["identity"]["uid"], "name": p["identity"]["username"], "score": p["score"]}
                  for p in participants.values() if p["eligible"]]}
    persistence = "queued"
    try:
        outbox.enqueue(record)
    except Exception:
        security.operational_failure("round_queue", logging.ERROR)
        persistence = "failed"
    await broadcast(room, {"type": "game_results", "round_id": round_state["id"], "scores": scores,
                           "winners": winners, "display_names": display_names, "persistence": persistence,
                           "ranked_players": [p["nickname"] for p in participants.values() if p["eligible"]]})


async def round_deadline(room):
    await asyncio.sleep(max(0, room["round"]["end"] - time.monotonic()))
    await finish_round(room)


@app.get("/")
def root():
    return {"message": "Speed Demon server running", "scoring_version": SCORING_VERSION}


@app.get("/leaderboard")
async def leaderboard(duration: int = 60, period: str = "weekly"):
    if duration not in DURATIONS or period not in ("weekly", "all_time"):
        raise HTTPException(400, "Invalid leaderboard selection")
    week = week_key(time.time()) if period == "weekly" else None
    key = (duration, period, week)
    async with leaderboard_lock:
        cached = leaderboard_cache.get(key)
        if cached and cached[0] > time.monotonic():
            if cached[1] is None:
                raise HTTPException(503, "Leaderboard temporarily unavailable")
            return cached[1]
        try:
            entries = await asyncio.to_thread(firebase.leaderboard, duration, period, week)
            entries = [{"name": entry["name"], "score": entry["score"]} for entry in entries[:10]]
        except Exception:
            security.operational_failure("leaderboard_read")
            leaderboard_cache[key] = (time.monotonic() + 15, None)
            raise HTTPException(503, "Leaderboard temporarily unavailable") from None
        response = {"entries": entries, "duration": duration, "period": period, "week": week, "scoring_version": SCORING_VERSION}
        if len(leaderboard_cache) > 32:
            leaderboard_cache.clear()
        leaderboard_cache[key] = (time.monotonic() + 30, response)
        return response


async def authenticate(player, token, validate_nickname=True):
    failure_message = None
    if token is None:
        identity = None
    elif not isinstance(token, str) or len(token) > 12000:
        raise ValueError("Invalid authentication token")
    else:
        if time.monotonic() - player["last_auth"] < 2:
            raise ValueError("Wait before authenticating again")
        player["last_auth"] = time.monotonic()
        try:
            async with auth_slots:
                identity = await asyncio.to_thread(firebase.verify_token, token)
        except Exception:
            security.operational_failure("token_verification")
            identity = None
            failure_message = "Google verification failed or unavailable. You can play as a guest."
    room = rooms.get(player["room"])
    if identity and room:
        username = identity.get("username")
        if username and any(p is not player and normalize_nickname(display_name(p)) == normalize_nickname(username) for p in room["players"]):
            raise ValueError("Your registered username is already in use in this room")
        if any(p is not player and p["identity"] and p["identity"]["uid"] == identity["uid"] for p in room["players"]):
            raise ValueError("This Google account is already in the room")
        if room["state"] == "playing" and any(p["player_id"] != player["id"] and p["identity"] and p["identity"]["uid"] == identity["uid"] for p in room["round"]["participants"].values()):
            raise ValueError("This Google account already participated in this round")
    player["identity"] = identity
    if room:
        await ensure_unique_identity(player, room)
    if room and room["state"] == "playing":
        participant = room["round"]["participants"][player["id"]]
        if not identity or not participant["identity"] or identity["uid"] != participant["identity"]["uid"]:
            participant["eligible"] = False
    await send_identity_status(player, failure_message)
    if room:
        await broadcast(room, lobby_message(room, "identity_changed"))
        if validate_nickname:
            await validate_guest_nickname(player)


async def send_identity_status(player, message=None):
    identity = player["identity"]
    room = rooms.get(player["room"])
    await send(player, {"type": "auth_status", "authenticated": identity is not None,
                        "username": identity.get("username") if identity else None, "eligibility": eligibility(player), "message": message,
                        "round_id": room["round"]["id"] if room and room["state"] == "playing" else None,
                        "ranked": room["round"]["participants"][player["id"]]["eligible"] if room and room["state"] == "playing" else None})


async def verified_profile(authorization):
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 12007:
        raise HTTPException(401, "Google authentication required")
    try:
        async with auth_slots:
            identity = await asyncio.to_thread(firebase.verify_token, authorization[7:])
        if identity["exp"] <= time.time():
            raise ValueError("Expired token")
        return identity
    except Exception:
        security.operational_failure("token_verification")
        raise HTTPException(401, "Google verification failed or unavailable") from None


@app.get("/profile")
async def get_profile(authorization: str | None = Header(default=None)):
    identity = await verified_profile(authorization)
    return JSONResponse({"authenticated": True, "username": identity.get("username")}, headers={"Cache-Control": "no-store"})


@app.post("/username")
async def register_username(payload: dict, authorization: str | None = Header(default=None)):
    try:
        normalized = validate_username(payload.get("username"))
    except UsernameError as error:
        raise HTTPException(400, str(error)) from None
    identity = await verified_profile(authorization)
    try:
        security.limiter.require(("REGISTER_UID", identity["uid"]))
    except security.RateLimitError as error:
        raise HTTPException(429, str(error), headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"}) from None
    existing = identity.get("username")
    if existing:
        if normalize_nickname(existing) != normalized:
            raise HTTPException(409, "Your username is permanent and cannot be changed.")
        return JSONResponse({"authenticated": True, "username": existing}, headers={"Cache-Control": "no-store"})
    try:
        async with auth_slots:
            username = await asyncio.to_thread(firebase.register_username, identity["uid"], payload.get("username"))
    except UsernameError as error:
        raise HTTPException(409 if error.code in ("username_taken", "username_permanent") else 400, str(error)) from None
    except Exception:
        security.operational_failure("username_registration")
        raise HTTPException(503, "Username registration unavailable. Retry the same username safely.") from None
    return JSONResponse({"authenticated": True, "username": username}, headers={"Cache-Control": "no-store"})


async def handle_message(player, message):
    message_type = message.get("type")
    try:
        security.validate_message(message)
        security.limit_control(player, message_type)
    except ValueError:
        if message_type == "authenticate":
            await authenticate(player, None, validate_nickname=False)
        raise
    if message_type == "authenticate":
        try:
            await authenticate(player, message.get("id_token"))
        except ValueError:
            await authenticate(player, None, validate_nickname=False)
            raise
        return
    if message_type == "refresh_profile":
        identity = player["identity"]
        if not identity or identity["exp"] <= time.time():
            raise ValueError("Verified Google authentication required")
        try:
            async with auth_slots:
                username = await asyncio.to_thread(firebase.get_username, identity["uid"])
        except Exception:
            raise ValueError("Username lookup unavailable. Try again.") from None
        room = rooms.get(player["room"])
        if username and room and any(p is not player and normalize_nickname(display_name(p)) == normalize_nickname(username) for p in room["players"]):
            raise ValueError("Your registered username is already in use in this room")
        identity["username"] = username
        if room:
            await ensure_unique_identity(player, room)
        await send_identity_status(player)
        if room:
            await broadcast(room, lobby_message(room, "identity_changed"))
            await validate_guest_nickname(player)
        return
    if message_type in ("create_room", "join_room"):
        if player["room"]:
            raise ValueError("Leave your current room first")
        if message_type == "create_room" and len(rooms) >= security.MAX_ROOMS:
            raise ValueError("Server is busy")
        if message_type == "join_room":
            code = message["room_code"]
            target = rooms.get(code.upper())
            if not target:
                raise ValueError("Room not found")
            if target["state"] != "lobby" or len(target["players"]) >= security.MAX_PLAYERS:
                raise ValueError("Room is unavailable for joining")
            if time.monotonic() - target["last_activity"] >= security.ROOM_IDLE_SECONDS:
                raise ValueError("This inactive room expired. Create or join another room.")
        await refresh_registered_identity(player)
        nickname = (player["identity"] or {}).get("username") or message.get("nickname")
        if not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 24 or any(ord(c) < 32 for c in nickname):
            raise ValueError("Nickname must contain 1–24 printable characters")
        player["nickname"] = nickname.strip()
        await validate_guest_nickname(player)
        if player["closed"] or player.get("closing"):
            raise ValueError("Connection is closing. Reconnect to play.")
        if message_type == "create_room":
            if len(rooms) >= security.MAX_ROOMS:
                raise ValueError("Server is busy")
            code = generate_room_code()
            room = {"code": code, "host": player["id"], "players": [], "state": "lobby", "round": None, "duration": 60, "last_activity": time.monotonic()}
            rooms[code] = room
        else:
            code = message.get("room_code")
            room = rooms.get(code.upper()) if isinstance(code, str) else None
            if room is not target:
                raise ValueError("Room changed while joining. Try again.")
            if not room:
                raise ValueError("Room not found")
            if room["state"] != "lobby":
                raise ValueError("Wait until the room returns to the lobby")
            if time.monotonic() - room["last_activity"] >= security.ROOM_IDLE_SECONDS:
                raise ValueError("This inactive room expired. Create or join another room.")
            if len(room["players"]) >= security.MAX_PLAYERS:
                raise ValueError("Room is full")
            if any(normalize_nickname(display_name(p)) == normalize_nickname(display_name(player)) for p in room["players"]):
                raise ValueError("That nickname is already in use")
            if player["identity"] and any(p["identity"] and p["identity"]["uid"] == player["identity"]["uid"] for p in room["players"]):
                raise ValueError("This Google account is already in the room")
        player["room"] = room["code"]
        room["players"].append(player)
        room["last_activity"] = time.monotonic()
        await send(player, {"type": "joined", "player_id": player["id"]})
        await broadcast(room, lobby_message(room, "room_created" if message_type == "create_room" else "player_joined"))
        return
    room = rooms.get(player["room"])
    if not room or player not in room["players"] or message.get("room_code") != room["code"]:
        raise ValueError("Room membership required")
    if message_type == "leave_room":
        await depart(player)
    elif message_type == "start_game":
        if room["host"] != player["id"]:
            raise ValueError("Only the host can start")
        duration = message.get("duration")
        if type(duration) is not int or duration not in DURATIONS:
            raise ValueError("Invalid duration")
        if room["state"] != "lobby" or len(room["players"]) < 2:
            raise ValueError("Start requires a lobby with at least two players")
        if time.monotonic() - room["last_activity"] >= security.ROOM_IDLE_SECONDS:
            raise ValueError("This inactive room expired. Create or join another room.")
        if len(round_tasks) >= security.MAX_ROUNDS:
            raise ValueError("Server is busy")
        members = list(room["players"])
        previous_names = [display_name(member) for member in members]
        for member in members:
            await refresh_registered_identity(member)
            await ensure_unique_identity(member, room)
        if previous_names != [display_name(member) for member in members]:
            await broadcast(room, lobby_message(room, "identity_changed"))
        identities = [(member["id"], display_name(member), dict(member["identity"] or {})) for member in members]
        for member in members:
            await validate_guest_nickname(member)
        if room["state"] != "lobby" or members != room["players"] or room["host"] != player["id"] or identities != [(member["id"], display_name(member), dict(member["identity"] or {})) for member in members]:
            raise ValueError("Room changed during identity verification. Try again.")
        start, start_wall = time.monotonic() + 3, time.time() + 3
        participants = {}
        for member in room["players"]:
            identity = member["identity"]
            verified = identity is not None and identity["exp"] > start_wall
            eligible = verified and bool(identity.get("username"))
            participants[member["id"]] = {"player_id": member["id"], "nickname": player_name(member),
                                           "display_name": display_name(member),
                                           "identity": dict(identity) if verified else None,
                                           "eligible": eligible, "score": 0, "next": 0, "issued": 32}
        names = [p["nickname"] for p in participants.values()]
        if len(set(names)) != len(names):
            for participant in participants.values():
                participant["nickname"] = participant["display_name"]
        room["duration"], room["state"] = duration, "playing"
        room["round"] = {"id": uuid.uuid4().hex, "start": start, "end": start + duration,
                         "start_wall": start_wall, "end_wall": start_wall + duration,
                         "participants": participants, "questions": generate_questions()}
        room["task"] = asyncio.create_task(round_deadline(room))
        round_tasks.add(room["task"])
        room["task"].add_done_callback(round_tasks.discard)
        started = {"type": "game_started", "round_id": room["round"]["id"], "duration": duration,
                   "server_time": time.time(), "start_at": start_wall, "end_at": start_wall + duration,
                   "questions": prompts(room["round"]["questions"])}
        await asyncio.gather(*(send(member, {**started, "ranked": participants[member["id"]]["eligible"]}) for member in list(room["players"])))
    elif message_type == "answer":
        round_state = room["round"]
        now = time.monotonic()
        if room["state"] != "playing" or message.get("round_id") != round_state["id"]:
            raise ValueError("Stale round")
        if now < round_state["start"] or now >= round_state["end"]:
            raise ValueError("Answer outside round time")
        participant = round_state["participants"][player["id"]]
        index = message.get("index")
        if type(index) is not int or index != participant["next"] or index >= participant["issued"]:
            raise ValueError("Duplicate or out-of-order answer")
        question = round_state["questions"][index]
        if message.get("question_id") != question["question_id"]:
            raise ValueError("Invalid question")
        answer = message.get("answer")
        if type(answer) not in (int, float) or abs(answer) > 1000000 or not math.isfinite(answer):
            raise ValueError("Invalid numeric answer")
        correct = answer == question["answer"]
        participant["score"] += 1 if correct else -1
        participant["next"] += 1
        tail = participant["issued"]
        if tail == len(round_state["questions"]):
            round_state["questions"].extend(generate_questions())
        participant["issued"] += 1
        await send(player, {"type": "answer_result", "round_id": round_state["id"], "index": index,
                            "correct": correct, "score": participant["score"],
                            "questions": prompts(round_state["questions"][tail:tail + 1], tail)})
    elif message_type == "play_again":
        if room["host"] != player["id"] or room["state"] != "results":
            raise ValueError("Only the host can replay a completed round")
        room["state"], room["round"] = "lobby", None
        room["last_activity"] = time.monotonic()
        await broadcast(room, lobby_message(room, "returned_to_lobby"))
    else:
        raise ValueError("Unsupported message; scores are calculated by the server")


async def reject_handshake(websocket, message, status=403, retry_after=None):
    if "websocket.http.response" in getattr(websocket, "scope", {}).get("extensions", {}):
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        await websocket.send_denial_response(JSONResponse({"detail": message}, status_code=status, headers=headers))
    else:
        await websocket.close(code=1008, reason=message)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ip = security.client_key(getattr(websocket, "scope", {}))
    try:
        security.limiter.require(("CONNECT_IP", ip), ("CONNECT_GLOBAL", "process"))
    except security.RateLimitError as error:
        await reject_handshake(websocket, str(error), 429, error.retry_after)
        return
    origins = websocket.headers.getlist("origin") if hasattr(websocket.headers, "getlist") else [websocket.headers.get("origin")]
    if len(origins) != 1 or origins[0] not in ALLOWED_ORIGINS:
        await reject_handshake(websocket, "WebSocket origin is not allowed.")
        return
    player = {"id": uuid.uuid4().hex, "websocket": websocket, "nickname": "", "room": None,
              "identity": None, "last_auth": -math.inf, "send_lock": asyncio.Lock(), "closed": False, "ip": ip,
              "task": asyncio.current_task(), "unjoined_since": time.monotonic()}
    # Reserve before accept yields, so simultaneous handshakes cannot exceed caps.
    if not claim_connection(player["id"], ip):
        await reject_handshake(websocket, "Connection capacity reached. Try again later.", 429, 30)
        return
    try:
        await websocket.accept()
        while not player["closed"] and not player.get("closing"):
            timeout = security.SOCKET_IDLE_SECONDS
            if not player["room"]:
                timeout = min(timeout, max(0, security.UNJOINED_SECONDS - (time.monotonic() - player["unjoined_since"])))
            event = await asyncio.wait_for(websocket.receive(), timeout)
            if player["closed"] or player.get("closing"):
                break
            if event["type"] == "websocket.disconnect":
                break
            raw = event.get("text")
            frame = raw if raw is not None else event.get("bytes", b"")
            invalid_text = False
            try:
                frame_size = len(raw.encode("utf-8")) if raw is not None else len(frame)
            except UnicodeError:
                frame_size, invalid_text = len(frame), True
            if frame_size > security.WS_MESSAGE_BYTES:
                await send(player, {"type": "error", "message": "WebSocket message too large.", "request_type": None})
                await websocket.close(code=1009, reason="Message too large")
                break
            message = None
            try:
                if invalid_text:
                    raise ValueError("Invalid text encoding")
                if raw is None:
                    raise ValueError("Text JSON messages required")
                try:
                    message = security.decode_json(raw)
                except (ValueError, UnicodeError, RecursionError):
                    raise ValueError("Invalid JSON message") from None
                if not isinstance(message, dict):
                    raise ValueError("Expected a JSON object")
                await handle_message(player, message)
            except (ValueError, TypeError, OverflowError) as error:
                kind = message.get("type") if isinstance(message, dict) else None
                kind = kind if isinstance(kind, str) and kind in security.MESSAGE_TYPES else None
                try:
                    security.limiter.require(("ERROR_SOCKET", player["id"]), ("ERROR_IP", ip))
                except security.RateLimitError:
                    await send(player, {"type": "error", "message": "Too many invalid or unauthorised messages. Reconnect later.", "request_type": kind})
                    await websocket.close(code=1008, reason="Excessive protocol errors")
                    break
                text = str(error) if isinstance(error, ValueError) else "Invalid protocol message"
                response = {"type": "error", "message": text, "request_type": kind}
                if isinstance(error, security.RateLimitError):
                    response["retry_after"] = error.retry_after
                await send(player, response)
    except asyncio.TimeoutError:
        await send(player, {"type": "error", "message": "Idle connection expired. Reconnect to play.", "request_type": None})
        try:
            await websocket.close(code=1001, reason="Idle connection expired")
        except (RuntimeError, OSError):
            pass
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        player["closed"] = True
        release_connection(player["id"])
        await depart(player)
