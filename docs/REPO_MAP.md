# Repository map

## Purpose and stack

Speed Demon is a multiplayer arithmetic race: players create or join a room, the host chooses a duration, and everyone answers the same buffered prompt sequence before comparing server-verified scores. Screen flow: home -> lobby -> countdown -> game -> results -> lobby.

- Backend: Python, FastAPI, Uvicorn, WebSockets and firebase-admin, pinned in `backend/requirements.txt`. Rooms and live rounds occupy one process; Google Authentication and Firestore support optional persistent rankings.
- Frontend: static HTML/CSS and plain browser JavaScript. No framework, npm manifest or bundler; only Firebase app/auth modules are dynamically imported from the official CDN, pinned to 13.0.0.
- Persistence: completed Firestore round records and per-UID best entries, with a local SQLite retry outbox. Guests do not require Firebase availability.

## Entry points and important files

| Path | Responsibility |
| --- | --- |
| `backend/main.py` | FastAPI app, origin/member/host checks, socket identity, room lifecycle, private question generation, authoritative deadlines/scoring and leaderboard HTTP cache. |
| `backend/firebase_service.py` | Lazy Admin initialization, verified Google identities, permanent profile/username transactions, week/version/duration board keys, idempotent round transactions and indexed top-10 queries. |
| `backend/persistence.py` | SQLite outbox, capped exponential retries, background persistence worker. |
| `backend/requirements.txt`, `backend/.env.example` | Pinned dependencies and non-secret configuration example. |
| `backend/render.yaml` | Existing empty file; deployment settings must be applied to the existing Render service manually. |
| `frontend/index.html` | Five screen sections, home-only Google account controls and leaderboard, native User Guide dialog and non-interactive SVG orbital/calculator background. Preserves labels, live feedback, skip link and named DOM bindings. |
| `frontend/app.js` | Socket protocol, verified profile and permanent registration UI, copy-code action, eligibility indicator, connection cleanup, host controls, prompt buffer, advisory timer, signed server scores, results/replay and leaderboard states. |
| `frontend/auth.js` | Optional Firebase SDK loading, popup Google login, logout and ID-token change notifications. Guest play survives SDK/config failure. |
| `frontend/config.js` | Public Firebase web configuration; localhost selects local backend, other hosts select existing Render service. No Admin secrets. |
| `frontend/style.css` | Black/graphite/silver responsive design, system sans typography, layered panel edges, CSS calculator depth, focused game status/question panels, account/leaderboard/dialog styling and focus/reduced-motion/hidden rules. No external fonts or image assets. |
| `firestore.rules`, `firestore.indexes.json`, `firebase.json` | Deny-all client rules, composite index and manual Firebase CLI deployment definitions. |
| `vercel.json` | Static `frontend` output with no install/build command; no domain ownership metadata. |
| `README.md`, `docs/FIREBASE_SETUP.md` | Public link, local commands, exact Firebase/Render/Vercel configuration, deploy procedure, cloud acceptance and free-tier limits. |
| `tests/test_round_flow.py`, `tests/test_persistence.py` | Isolated handler/security, Firebase adapter/transaction, caching, rollover and outbox tests. |
| `tests/test_frontend_replay.cjs`, `tests/test_frontend_auth.cjs` | Real client and auth script tests with DOM/socket/timer/SDK doubles. |
| `tests/test_frontend_browser.cjs` | Optional real Edge/Playwright smoke test against isolated local servers; guest rounds, SDK-fake account layout, responsive screenshots, leaderboard fixtures, results and replay. Uses an existing Playwright installation. |
| `tests/test_usernames.py`, `tests/test_feature_browser.cjs` | Concurrent Firestore-transaction fakes, permanent names/profile endpoints, scoring-version isolation; real local Edge/Chrome registration, ranking eligibility, negative persistence and replay. |
| `tests/local_server.py` | Real local Uvicorn server backed by in-memory Firebase fakes; cannot access production Firebase. |
| `AGENTS.md` | Project-specific instructions for future Codex work. |

Virtual environments, caches, Git internals, ignored `test-artifacts`, generated files and the Windows shortcut are not architecture sources.

## Backend interface and state

- `GET /`: health message and scoring version; does not serve frontend files.
- `GET /profile`, `POST /username` (JSON `username`): require a Firebase Google ID token in the `Authorization: Bearer` header. UID comes exclusively from verified claims. Responses expose only authentication status and registered username, with `Cache-Control: no-store`. Registration returns 400 for invalid/reserved names, 409 for conflicts/permanent-name changes, 503 for transaction failure. Profile/authentication failure returns 401. CORS permits these requests only from the existing allowed origins.
- `GET /leaderboard?duration=60&period=weekly`: public top-10 names/scores plus duration, period, week and version. Period accepts `weekly`/`all_time`; duration accepts 60/120/300/600. Invalid selection returns 400; database/config failure returns 503. Thirty-second success cache, fifteen-second failure cache; request coalescing avoids duplicate queries. Weekly cache keys change at Monday 00:00 Johannesburg.
- WebSocket `/ws`: accepts JSON objects with a `type` discriminator only from explicitly allowed origins. Each connection gets a random player ID; no browser UID/nickname assertion grants authority. One socket may occupy one room at a time.

| Client message | Fields besides `type` | Response/broadcast |
| --- | --- | --- |
| `authenticate` | `id_token`: Firebase ID token or null for guest/logout | Private `auth_status`: authenticated/username/eligibility/message. Room `identity_changed` updates public player status. UID/token/Google display names never broadcast. |
| `refresh_profile` | None | Looks up the username using the socket's verified, unexpired identity; sends `auth_status` and room `identity_changed`. Does not upgrade an active round's frozen eligibility. |
| `create_room` | `nickname` | Private `joined` with player_id, then `room_created` with room_code/players/host. |
| `join_room` | `room_code`, `nickname` | Private `joined`, then room `player_joined`; unique nickname/UID, lobby-only, eight-player maximum. |
| `start_game` | `room_code`, `duration` | Host-only, lobby-only, at least two players. Private `game_started`: round_id, duration, server_time, start_at, end_at, prompt buffer, own ranked eligibility. |
| `answer` | `room_code`, `round_id`, `question_id`, `index`, `answer` | Private `answer_result`: round_id/index, correctness, authoritative total score and one new buffered prompt. Invalid, duplicate, out-of-order, unissued, stale, countdown or late answers rejected. |
| `play_again` | `room_code` | Host-only after completion; `returned_to_lobby` retains current members and room, clears round state. |
| `leave_room` | `room_code` | Membership-bound departure; `player_left` to remaining players. Frontend Back closes its socket. |

All room/host actions check the socket's actual membership and host player ID. Client `submit_score` is unsupported. Errors are private `error` messages with a request_type; token verification failures use sanitized status messages. Repeated starts/replay cannot replace an active round. Malformed payloads are rejected without terminating valid room state. Limits include socket/room/round counts, message size/rate, idle and send timeouts.

`rooms` maps random six-character codes to host ID, players, duration, state (`lobby`, `playing`, `results`), round and deadline task. Players contain server ID, nickname, socket, verified identity/expiry, membership and send lock. A round has UUID, monotonic start/deadline, scheduled wall timestamps, private shared questions, frozen participant identities/eligibility and per-player score/next/issued counters. Reservation/profile reads happen before room admission or round snapshots. Round start rechecks membership and identity after asynchronous validation; answer mutations still validate and update without an intervening await. Sends are serialized per socket. `round_tasks` tracks deadlines even if every player leaves; shutdown cancels them.

Round countdown lasts three seconds. Server monotonic time governs accepted answers and an independent task ends the round without score submissions. Correct answers earn +1; valid wrong answers earn −1; negative totals are allowed. Each accepted answer consumes one question. Invalid, duplicate or late answers do not change scores. Prompts contain no answer key; question IDs/indexes bind submissions to the issued sequence. The initial lookahead is 32 questions, replenished by acknowledgements, with a shared sequence generated further as required. Results include all original participants and explicit ties, even if someone left; departures disqualify that person's ranking and transfer host ownership. Empty rooms are removed, but their deadline still records completion.

Authentication is optional. The Admin SDK checks Google provider, project/signature/expiry and revocation, then looks up the registered username. Ranked play requires both a valid Google identity and a registered username. Start freezes identity, username and eligibility, including expiry at the scheduled start. Late sign-in or registration never adds eligibility; logout/account switch/token failure/disconnect can revoke it. Duplicate UIDs are rejected across current room members and original verified active-round participants, including unregistered accounts. Registered names override browser nicknames. Replay takes a new snapshot.

## Frontend interaction and game data flow

1. DOM IDs, CSS `.hidden` and `showScreen` preserve the home/lobby/countdown/game/results flow and focus transitions. `app-shell`'s `data-screen` controls spacing/background treatment. Account controls and the leaderboard are nested inside home, so countdown/game/lobby/results fully hide them. CSS also hides `.atmosphere` throughout countdown/game. The native User Guide dialog supports Escape and restores focus. Responsive controls, live feedback and reduced motion remain.
2. `enterRoom` validates nickname/code, closes prior sockets and creates one connection. After opening, it sends an optional ID token followed by create/join. `joined` sets the server player ID; room events render player rows and ID-based host controls. Back closes the socket. Errors/disconnects are shown as notices; reconnect is a fresh lobby join, never an active-round restoration.
3. `auth.js` loads only Firebase app/auth modules. `onIdTokenChanged` dispatches `auth-changed`; the app transports refreshed tokens privately to WebSocket and profile endpoints. A missing/broken SDK leaves guest play available. Signed-in unregistered players get a compact home prompt and a native registration dialog with a warning beneath the field, an explicit permanence acknowledgement, and a final confirmation dialog showing the exact username. The nickname field displays the verified username read-only for registered users. Lobby labels (Ranked, Guest · Unranked, Username required) use backend member eligibility, with no Google identity banner. Registration refreshes a connected socket profile without adding active-round eligibility.
4. `game_started` resets local state, receives prompts without answers and computes advisory monotonic display times from server timestamps. The server ends the round independently. The client counts down, focuses answer input and renders progress/urgent time in minutes:seconds. Active UI shows own confirmed score and room code; the protocol provides no live opponent scores. Operand/operator text nodes allow distinct operator styling without HTML interpolation; question count has no fixed limit.
5. Enter (excluding key repeat) and Submit validate numeric input and send question/round IDs, index and answer. The client immediately advances through the buffer; it awards no points. `answer_result` updates score/feedback and adds prompts. Empty buffer temporarily disables controls; an acknowledgement restores focus. Stale acknowledgements/results from another round are ignored. An answer rejection stops local answering and waits for final server results.
6. Local display expiry disables input and shows waiting results; it sends no score. `game_results` renders server scores/ties and persistence status. Replay cancels the display timer and returns both clients to a fresh lobby. Auth/leave status updates do not restart a round.
7. Leaderboard tabs, native duration select and Refresh issue HTTP requests without polling. Loading/empty/error states are explicit, stale requests are aborted/ignored and names are rendered as text nodes. Arrow/Home/End keys switch tabs with proper focus/ARIA selection. The list uses server tie ordering.

## Persistence and rankings

Firestore data lives in `rounds/{round_id}` and `boards/{version_duration_week-or-all}/entries/{sha256(uid)}`. A single idempotent transaction writes the completion marker, embedded authenticated results and best-score improvements. It reads all documents before writing, so concurrent retries don't duplicate results. Completed guest rounds retain metadata without guest names or email/token/key fields.

Permanent usernames use `profiles/{sha256(uid)}` (username, normalized_username, created_at) and `usernames/{lowercase_username}` (profile_id). A five-attempt Firestore transaction reads both records before creating either; one profile per UID and one claim per normalized name prevent both duplicate names and multiple names for an account. Names use 3–20 ASCII letters/numbers/underscores; admin/administrator/moderator/system/support are reserved case-insensitively. Retrying the same normalized name returns the original spelling. Different-name attempts cannot mutate the existing profile. Client Firestore access remains denied. No new composite index is required for direct profile/claim reads. Guest/unregistered room entry, in-room identity changes and each new round check normalized nickname reservations server-side and fail closed on lookup failures. Entry/start also refresh authenticated profiles. One point read is added per guest reservation check and per authenticated profile refresh, with none per answer. Public lobby `display_name` and result `display_names` keep guest identities prefixed and round snapshots stable; score keys remain compatible when unique. Registration races do not grant guests canonical identities, and the next validation rejects a newly claimed guest name. See `docs/FIREBASE_SETUP.md` for the operator-only correction/deletion procedure.

Weekly keys identify the Monday date at UTC+02:00 (Africa/Johannesburg, no DST); the scheduled **start** chooses the week even if completion/retry crosses midnight. Duration and `arithmetic-v3` separate the current +1/−1 rankings. Boards start fresh; earlier scores are not carried over. Existing documents are not deleted or migrated, and pending older records retain their original version so they cannot enter current rankings. Top-10 query: score descending, achieved_at ascending, UID ascending; negative best scores are valid and one player's equal best keeps the earliest completion (then source round ID). The existing entries composite index still applies.

`ResultOutbox` enqueues records in local SQLite and retries Firestore failures with exponential backoff capped at five minutes. Firebase access is offloaded to threads, so gameplay remains independent. Successfully committed records survive restarts in Firestore. The queue survives only while local disk survives; free Render ephemeral storage can lose pending records during restart/redeploy. Queue/disk errors surface separately from game results. Secrets are supplied by environment/file only; client Firestore rules remain deny-all.

## Running and verification

Run from repository root with existing dependencies, in separate terminals:

```powershell
.\backend\venv\Scripts\python.exe -B -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --ws-max-size 16384
```

```powershell
python -B -m http.server 5500 --bind 127.0.0.1 --directory frontend
```

Open `http://localhost:5500`. The public config automatically chooses localhost:8000 for local pages; hosted pages use the existing `https://speed-demom.onrender.com` backend. Use one worker. To avoid production Firebase access during browser testing, run `python -B -m tests.local_server` instead of the Uvicorn command; it uses isolated fakes, rejects real tokens and removes temporary queue data on shutdown.

Focused commands:

```powershell
.\backend\venv\Scripts\python.exe -B -m unittest discover -s tests -v
node --check frontend/app.js
node --check frontend/auth.js
node --check frontend/config.js
node tests/test_frontend_replay.cjs
node tests/test_frontend_auth.cjs
git diff --check
```

Use `.venv` or `python` instead if dependencies are available there. No additional test packages are needed. Handler/Firestore/auth tests use fakes, while the real browser smoke server exercises real HTTP/WebSocket transport and UI with a fake database. Neither proves deployed Auth/IAM/index/Firestore integration. See `docs/FIREBASE_SETUP.md` for manual settings and acceptance steps.

The backend suite includes 640 ordered submissions per duration (60/120/300/600 seconds), with simulated backend time, 32-question lookahead, private answer keys, −1 wrong-answer consumption, duplicate rejection and exact-deadline enforcement. The buffer is unchanged.

For optional browser screenshots, start both local servers above, set `PLAYWRIGHT_MODULE_PATH` to an existing Playwright package path if it is not on Node's module search path, then run `node tests/test_frontend_browser.cjs`. It uses installed Edge by default (`BROWSER_CHANNEL` can override), writes ignored `test-artifacts/graphite-*.png`, and runs one actual 60-second round. Checks include short portrait/landscape controls, long account names/prompts, guide/tab keyboard focus, hidden game decoration and computed select-option contrast. Account layout uses isolated Firebase SDK modules; leaderboard populated/error states use explicitly labelled HTTP fixtures. Primary screenshots use the real, empty local leaderboard and guest rounds. Computed option contrast does not prove native OS popup appearance. No production Firebase data is accessed.

## Known limitations and verification boundaries

- Live rooms cannot survive backend restart or span workers/instances. Departed players are not reattached to active rounds; completion retains their server score but disqualifies rankings.
- Free Render cold starts and ephemeral disk remain hosting constraints. Pending persistence can be lost on restart; committed Firestore data is durable. Database/cache outages delay or temporarily hide rankings. Reservation/profile lookup outages block room entry/new rounds; active answers remain independent of Firestore.
- The browser timer is advisory; network transit can make it trail the server deadline. Only server-received, on-time answers count. Buffering hides per-answer latency but cannot accept late answers.
- Server scoring prevents fabricated totals/impersonation, not automated solving or coordinated account abuse. Distributed abuse mitigation is outside this implementation.
- The owner confirmed production domain `speed-demons.vercel.app` and the external Firebase/Render/Vercel configuration. Render tracks `main` with auto-deploy disabled. `vercel.json` declares static output; hosted behavior still requires post-deployment acceptance.
- Real Google login, Render secret-file/IAM configuration, Firestore index readiness and live writes remain unverified without valid credentials and production-test approval. Automated tests must remain isolated.

Consult this map for navigation, then verify details against the source. Update it when paths, endpoints, protocol, state ownership, dependencies or startup/test procedures change.

Feature browser checks: with isolated local servers running, use `node tests/test_feature_browser.cjs` with the same `PLAYWRIGHT_MODULE_PATH`; run once with default Edge, then `BROWSER_CHANNEL=chrome`. The smoke server accepts only the two explicit local test tokens (`local-smoke-account-a`/`local-smoke-account-b`), never real credentials. It provides in-memory profiles/claims and version-specific boards. Real Firebase transaction concurrency uses isolated optimistic-retry fakes in the Python suite, not the browser smoke database. Native dropdown screenshots and real Clipboard copy are exercised in both browsers. No production Firebase or cloud settings are used.
