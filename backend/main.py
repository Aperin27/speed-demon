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
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .firebase_service import DURATIONS, SCORING_VERSION, UsernameError, firebase, normalize_nickname, week_key
from .persistence import outbox

ALLOWED_ORIGINS = {origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "https://speed-demons.vercel.app,http://localhost:5500,http://127.0.0.1:5500").split(",")}
rooms = {}
connections = set()
round_tasks = set()
leaderboard_cache = {}
leaderboard_lock = asyncio.Lock()
auth_slots = asyncio.Semaphore(4)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    outbox.start()
    yield
    tasks = list(round_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await outbox.stop()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


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
    if not room or player not in room["players"]:
        return
    room["players"].remove(player)
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
        logger.error("Could not queue completed round")
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
        except Exception:
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
        raise HTTPException(401, "Google verification failed or unavailable") from None


@app.get("/profile")
async def get_profile(authorization: str | None = Header(default=None)):
    identity = await verified_profile(authorization)
    return JSONResponse({"authenticated": True, "username": identity.get("username")}, headers={"Cache-Control": "no-store"})


@app.post("/username")
async def register_username(payload: dict, authorization: str | None = Header(default=None)):
    identity = await verified_profile(authorization)
    try:
        username = await asyncio.to_thread(firebase.register_username, identity["uid"], payload.get("username"))
    except UsernameError as error:
        raise HTTPException(409 if error.code in ("username_taken", "username_permanent") else 400, str(error)) from None
    except Exception:
        raise HTTPException(503, "Username registration unavailable. Retry the same username safely.") from None
    return JSONResponse({"authenticated": True, "username": username}, headers={"Cache-Control": "no-store"})


async def handle_message(player, message):
    message_type = message.get("type")
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
        await refresh_registered_identity(player)
        nickname = (player["identity"] or {}).get("username") or message.get("nickname")
        if not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 24 or any(ord(c) < 32 for c in nickname):
            raise ValueError("Nickname must contain 1–24 printable characters")
        player["nickname"] = nickname.strip()
        await validate_guest_nickname(player)
        if message_type == "create_room":
            if len(rooms) >= 1000:
                raise ValueError("Server is busy")
            code = generate_room_code()
            room = {"code": code, "host": player["id"], "players": [], "state": "lobby", "round": None, "duration": 60}
            rooms[code] = room
        else:
            code = message.get("room_code")
            room = rooms.get(code.upper()) if isinstance(code, str) else None
            if not room:
                raise ValueError("Room not found")
            if room["state"] != "lobby":
                raise ValueError("Wait until the room returns to the lobby")
            if len(room["players"]) >= 8:
                raise ValueError("Room is full")
            if any(normalize_nickname(display_name(p)) == normalize_nickname(display_name(player)) for p in room["players"]):
                raise ValueError("That nickname is already in use")
            if player["identity"] and any(p["identity"] and p["identity"]["uid"] == player["identity"]["uid"] for p in room["players"]):
                raise ValueError("This Google account is already in the room")
        player["room"] = room["code"]
        room["players"].append(player)
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
        if len(round_tasks) >= 1000:
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
        if type(answer) not in (int, float) or not math.isfinite(answer) or abs(answer) > 1000000:
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
        await broadcast(room, lobby_message(room, "returned_to_lobby"))
    else:
        raise ValueError("Unsupported message; scores are calculated by the server")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if websocket.headers.get("origin") not in ALLOWED_ORIGINS or len(connections) >= 2000:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    player = {"id": uuid.uuid4().hex, "websocket": websocket, "nickname": "", "room": None,
              "identity": None, "last_auth": -math.inf, "send_lock": asyncio.Lock(), "closed": False}
    connections.add(player["id"])
    arrivals = deque()
    try:
        while not player["closed"]:
            raw = await asyncio.wait_for(websocket.receive_text(), 900)
            now = time.monotonic()
            while arrivals and arrivals[0] < now - 1:
                arrivals.popleft()
            arrivals.append(now)
            if len(raw) > 16384 or len(arrivals) > 80:
                await websocket.close(code=1008)
                break
            message = None
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("Expected a JSON object")
                await handle_message(player, message)
            except (ValueError, TypeError, OverflowError) as error:
                await send(player, {"type": "error", "message": str(error),
                                    "request_type": message.get("type") if isinstance(message, dict) else None})
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
        pass
    finally:
        player["closed"] = True
        connections.discard(player["id"])
        await depart(player)
