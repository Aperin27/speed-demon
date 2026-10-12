# Speed Demon

Play at [speed-demons.vercel.app](https://speed-demons.vercel.app/).
The owner has confirmed this production domain and the existing Firebase, Render and Vercel configuration. Live Firebase connectivity and authenticated persistence still require post-deployment verification.

Race friends through arithmetic questions in 60, 120, 300 or 600-second rounds. Guests can create, join and play with unreserved nicknames, displayed as Guest · Nickname. Backend Firestore access is required to verify room-entry nickname reservations. Ranked play requires verified Google sign-in and a permanent registered username before the round starts. Correct answers earn +1; incorrect answers lose 1; negative scores are allowed. Scores and deadlines are enforced by the server. Weekly and All-Time top-10 boards use the new `arithmetic-v3` scoring version and start fresh without carrying over earlier scores.

The Render free service may need about 30 seconds to wake. Rooms live in one process; a restart interrupts active games.

## Local setup

Python 3.13 and Node.js are suitable for the checked-in verification commands. No frontend build or npm installation is needed. With approval to install backend dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe -B -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --ws-max-size 16384 --no-proxy-headers --no-access-log
```

In a second terminal:

```powershell
python -B -m http.server 5500 --bind 127.0.0.1 --directory frontend
```

Open `http://localhost:5500/` (localhost is authorized for Firebase Google sign-in). The local frontend automatically targets localhost:8000. Backend commands run from the repository root; the former `cd backend; uvicorn main:app` command is superseded by the package imports.

For an entirely isolated guest/browser test, replace the Uvicorn command with `python -B -m tests.local_server`. This server never calls Firebase, refuses real tokens, stores results only in temporary local storage and returns fake leaderboards. Use this mode for smoke tests without production-write approval.

## Verification

Using the existing backend environment or `.venv` with installed requirements:

```powershell
.\backend\venv\Scripts\python.exe -B -m unittest discover -s tests -v
node --check frontend/app.js
node --check frontend/auth.js
node --check frontend/config.js
node tests/test_frontend_replay.cjs
node tests/test_frontend_auth.cjs
git diff --check
```

Python tests exercise the real WebSocket handler, Firebase adapter transactions and retry queue with isolated fakes. Node checks execute the real client and authentication lifecycle with DOM/socket/SDK doubles. They do not prove live Google login, IAM access or production index readiness. See [the repository map](docs/REPO_MAP.md) for the protocol and [Firebase setup](docs/FIREBASE_SETUP.md) for exact manual configuration, deploy procedure and cloud verification.

## Deployment

Keep the existing Vercel frontend and Render backend. Vercel serves `frontend/` without a build. Configure the existing Render service's root as the repository root, build with `pip install -r backend/requirements.txt`, and start with `uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1 --ws-max-size 16384`.

The public Firebase web configuration is in `frontend/config.js`. Admin credentials belong only in Render secret files/environment settings. Client Firestore access stays denied. Follow [docs/FIREBASE_SETUP.md](docs/FIREBASE_SETUP.md) before deploying; no deployment or credential provisioning is performed by these source changes.

See [backend security controls](docs/SECURITY.md) for rate limits, idle-room cleanup, fast-answer exemptions and required trusted-proxy configuration before any approved deployment.
