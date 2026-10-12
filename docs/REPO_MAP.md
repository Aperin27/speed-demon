# Repository map

## Purpose and stack

Speed Demon is a multiplayer arithmetic race: players create or join a room, the host chooses a round duration, and players answer a shared question sequence before comparing scores. The intended screen flow is home -> lobby -> countdown -> game -> results -> lobby; see the implementation concerns below for current limitations.

- Backend: Python, FastAPI, Uvicorn, and WebSockets; declared in `backend/requirements.txt` without version pins. Python's standard library supplies random question generation and JSON serialization.
- Frontend: static HTML, CSS, and browser JavaScript, with no framework, bundler, npm manifest, or external client library.
- No database, authentication service, or persistent storage. Room state lives in one backend process.

## Entry points and important files

| Path | Responsibility |
| --- | --- |
| `backend/main.py` | FastAPI `app`, room storage, question generation, WebSocket dispatch, score aggregation, and disconnect handling. All backend logic is here. |
| `backend/requirements.txt` | Declares `fastapi`, `uvicorn`, and `websockets`. |
| `backend/render.yaml` | Empty file; currently supplies no deployment configuration. |
| `frontend/index.html` | Five screen containers, inputs, buttons, and DOM IDs; loads `style.css` and then `app.js` at the end of the body. |
| `frontend/app.js` | DOM bindings, socket connection and event dispatch, screen transitions, host controls, countdown, answer checking, timer, and results. One classic script, not separate imported modules. |
| `frontend/style.css` | Shared layout and controls, screen/card styling, and `.hidden` visibility class. |
| `README.md` | Public game link and a note about backend cold-start delay; no local setup or test instructions. |
| `AGENTS.md` | Project-specific instructions for future Codex work. |

Virtual environments, caches, Git internals, generated files, and the Windows shortcut are not architecture sources.

## Backend interface and state

`backend/main.py` defines two application endpoints:

- `GET /`: returns a JSON server-running message; it does not serve the frontend.
- WebSocket `/ws`: accepts JSON text messages with a `type` field. `websocket_endpoint()` dispatches them; `send_to_room()` broadcasts JSON to connected players and removes players whose sends fail.

| Client message | Fields besides `type` | Server response/broadcast |
| --- | --- | --- |
| `create_room` | `nickname` | `room_created`: `room_code`, `players`, `host` |
| `join_room` | `room_code`, `nickname` | `player_joined`: `room_code`, `players`, `host`; `error` for missing room |
| `start_game` | `room_code`, `duration` | `game_started`: `duration`, `questions`; host nickname check |
| `submit_score` | `room_code`, `nickname`, `score` | `game_results`: `scores`, `winner`, when score count equals current player count |
| `play_again` | `room_code` | `returned_to_lobby`: `room_code`, `players`, `host`; only the room's host socket can replay |

Other outbound messages are `player_left` (`players`, `host`) and `error` (`message`).

The global `rooms` dictionary is keyed by unique four-character uppercase/digit codes. Each room contains `host`, `players` (nickname/socket pairs), `scores` (nickname keys), `started`, `duration`, and `questions`. Each socket handler remembers `current_room` and `current_nickname`. Disconnect cleanup removes players by nickname and deletes an empty room; it does not transfer host ownership.

Starting a round sets `started`, duration, and questions, and clears scores. Score submissions accumulate independently of their order. Only `play_again` performs the replay reset: it clears `started`, scores, and questions while preserving room code, host, players, and selected duration. Replay checks the socket's current room and the first host-named player's socket. Unrecognized message types are ignored.

## Frontend interaction and game data flow

All client behavior is in `frontend/app.js`:

1. DOM constants bind to IDs in `frontend/index.html`. `showScreen()` toggles `.hidden` from `frontend/style.css`. UI changes must keep these bindings aligned.
2. `createRoom()` / `joinRoom()` validate basic input, call `connectWebSocket()`, then send their request on socket open. Lobby events update `roomCode`, `players`, and `host`; `updatePlayersList()` and `updateHostControls()` render them.
3. `requestStartGame()` sends the selected duration (HTML offers 60, 120, 300, or 600 seconds). `generate_questions()` creates 200 questions using operands 1-12 and addition, subtraction, or multiplication; answers are included in the broadcast.
4. `game_started` loads the shared sequence and starts a browser-local three-second countdown. `startGame()` resets local score/index and starts a one-second interval. `showCurrentQuestion()` supplies the displayed question and expected answer.
5. `submitAnswer()` compares `Number(answerInput.value)` with the supplied answer, awards one point for a match, and advances even on a wrong answer. Button click and Enter share this function. Exhausting questions displays "No more questions" until time expires.
6. `endGame()` submits the browser's final score and displays a waiting screen. The backend chooses the maximum submitted score; `renderResults()` displays the broadcast. Equal scores select the first maximum in dictionary order, without an explicit tie presentation.
7. Play Again sends `play_again`. `returned_to_lobby` cancels countdown/game timers, clears local questions and score state, refreshes room membership and host controls, and shows the lobby on both clients. The next start initializes a fresh round.

State is mutable script-level variables: socket/identity/lobby data, question sequence/index, score, expected answer, duration/time remaining, and timer handle. There is no centralized store, server-controlled deadline, or server-side answer checking.

## Running and verification

With Python and the declared backend dependencies already available, run from the repository root in PowerShell, in separate terminals:

```powershell
Set-Location backend
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

```powershell
Set-Location frontend
python -m http.server 5500 --bind 127.0.0.1
```

Open `http://127.0.0.1:5500/`. These commands are derived from the source layout, not a checked-in start script, and were not run during documentation creation. An existing `backend/venv` is local environment material, not a reproducible setup specification.

**Connection caveat:** `BACKEND_WS_URL` in `frontend/app.js` points to `wss://speed-demom.onrender.com/ws`, so serving the frontend locally still uses that deployed backend. A fully local session requires changing it to `ws://127.0.0.1:8000/ws`. `BACKEND_HTTP_URL` points to the same deployed service but is unused. This map does not change either constant. `README.md` links to a Vercel-hosted frontend; deployment availability was not checked.

Focused regression checks require the existing backend dependencies and Node.js, with no additional test packages:

```powershell
.\backend\venv\Scripts\python.exe -B -m unittest discover -s tests -v
node --check frontend/app.js
node tests/test_frontend_replay.cjs
```

Use `python` instead of the virtual-environment executable if the dependencies are available there. `tests/test_round_flow.py` exercises the real WebSocket handler with in-memory socket doubles: both score orders, replay and second rounds, rejected guest start/replay, duplicate-host-name replay rejection, missing rooms, and unrelated messages. `tests/test_frontend_replay.cjs` executes the real client script with DOM/socket/timer doubles to check replay messages, lobby transitions, timer cancellation, and subsequent scoring. These are automated handler/client checks, not real-network or browser tests. No lint configuration or build scripts are configured.

For browser verification, use two local tabs and a temporary frontend copy whose backend URLs point to localhost; discard the copy afterward and retain the deployed constants in repository source. Exercise create/join, start, answers, results, host replay, both lobbies, and a second round. Score order should be verified separately with controlled submissions. Disconnect behavior needs separate verification.

## Known implementation concerns

Remaining concerns from source inspection; these are outside the replay fix and are not a passing runtime validation report:

- The client does not handle `player_left` events.
- Nicknames are used as identity, start-game authorization, and score keys without uniqueness enforcement. Replay checks the host socket, but submitted scores and start-game target rooms remain trusted without socket-membership or value validation; clients receive answers. Duplicate/spoofed names or scores can disrupt results.
- Rooms disappear on restart and cannot be shared across independent workers. Host departure leaves the old host name; disconnects do not re-evaluate result completion, potentially leaving players waiting.
- Socket errors/reconnects are not handled. Repeated connections and Back do not close prior sockets; countdown/timer lifecycle is not guarded against repeated starts. The waiting-score display interpolates the nickname into `innerHTML`.
- CORS is broadly permissive; WebSocket origin/authentication checks are absent. Dependencies are unpinned and deployment configuration is empty.

Consult this map to locate code, then verify behavior in the actual source before editing. Update it when paths, endpoints, protocol, state ownership, dependencies, or startup/test procedures change.
