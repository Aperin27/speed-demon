from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import random
import string
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

rooms = {}

def generate_room_code():
    characters = string.ascii_uppercase + string.digits

    while True:
        code = ''.join(random.choice(characters) for _ in range(4))
        if code not in rooms:
            return code

def generate_questions(count=200):
    questions = []

    for _ in range(count):
        num1 = random.randint(1, 12)
        num2 = random.randint(1, 12)
        operator = random.choice(["+", "-", "*"])

        if operator == "+":
            question_text = f"{num1} + {num2}"
            answer = num1 + num2
        elif operator == "-":
            question_text = f"{num1} - {num2}"
            answer = num1 - num2
        else:
            question_text = f"{num1} × {num2}"
            answer = num1 * num2

        questions.append({
            "question": question_text,
            "answer": answer
        })

    return questions

async def send_to_room(room_code, message):
    if room_code not in rooms:
        return

    disconnected = []

    for player in rooms[room_code]["players"]:
        websocket = player.get("websocket")
        if websocket:
            try:
                await websocket.send_text(json.dumps(message))
            except:
                disconnected.append(player)

    for player in disconnected:
        rooms[room_code]["players"].remove(player)

@app.get("/")
def root():
    return {"message": "Speed Demon server running"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    current_room = None
    current_nickname = None

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            message_type = message.get("type")

            if message_type == "create_room":
                nickname = message.get("nickname")
                room_code = generate_room_code()

                rooms[room_code] = {
                    "host": nickname,
                    "players": [
                        {
                            "nickname": nickname,
                            "websocket": websocket
                        }
                    ],
                    "scores": {},
                    "started": False,
                    "duration": 60,
                    "questions": []
                }

                current_room = room_code
                current_nickname = nickname

                await websocket.send_text(json.dumps({
                    "type": "room_created",
                    "room_code": room_code,
                    "players": [nickname],
                    "host": nickname
                }))

            elif message_type == "join_room":
                room_code = message.get("room_code", "").upper()
                nickname = message.get("nickname")

                if room_code not in rooms:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Room not found"
                    }))
                    continue

                room = rooms[room_code]

                room["players"].append({
                    "nickname": nickname,
                    "websocket": websocket
                })

                current_room = room_code
                current_nickname = nickname

                player_names = [player["nickname"] for player in room["players"]]

                await send_to_room(room_code, {
                    "type": "player_joined",
                    "room_code": room_code,
                    "players": player_names,
                    "host": room["host"]
                })

            elif message_type == "start_game":
                room_code = message.get("room_code", "").upper()
                duration = message.get("duration", 60)

                if room_code not in rooms:
                    continue

                room = rooms[room_code]

                if current_nickname != room["host"]:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Only the host can start the game"
                    }))
                    continue

                room["started"] = True
                room["duration"] = duration
                room["scores"] = {}
                room["questions"] = generate_questions()

                await send_to_room(room_code, {
                    "type": "game_started",
                    "duration": duration,
                    "questions": room["questions"]
                })

            elif message_type == "submit_score":
                room_code = message.get("room_code", "").upper()
                nickname = message.get("nickname")
                score = message.get("score")

                if room_code not in rooms:
                    continue

                room = rooms[room_code]
                room["scores"][nickname] = score

                player_count = len(room["players"])
                submitted_count = len(room["scores"])

                if submitted_count == player_count:
                    scores = room["scores"]
                    winner = max(scores, key=scores.get)

                    await send_to_room(room_code, {
                        "type": "game_results",
                        "scores": scores,
                        "winner": winner
                    })

    except WebSocketDisconnect:
        if current_room and current_room in rooms:
            room = rooms[current_room]
            room["players"] = [
                player for player in room["players"]
                if player["nickname"] != current_nickname
            ]

            if room["players"]:
                player_names = [player["nickname"] for player in room["players"]]

                await send_to_room(current_room, {
                    "type": "player_left",
                    "players": player_names,
                    "host": room["host"]
                })
            else:
                del rooms[current_room]