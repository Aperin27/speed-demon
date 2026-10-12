# Project instructions

## Architecture navigation

- Consult `docs/REPO_MAP.md` for unfamiliar architecture, cross-file changes, WebSocket/game-flow work, or startup and verification questions. Small isolated edits can go directly to the relevant source.
- Treat the map as navigation guidance. Verify relevant paths, behavior, and assumptions against actual source before changing anything.
- Update `docs/REPO_MAP.md` when architectural changes invalidate its file responsibilities, endpoints, message protocol, state/data flow, dependencies, or startup/test guidance.

## Stack and conventions

- Backend: Python with FastAPI, Uvicorn, WebSockets and firebase-admin. `backend/main.py` owns in-memory rooms and authoritative rounds; `backend/firebase_service.py` verifies Google ID tokens and performs Firestore transactions; `backend/persistence.py` retries completed records through a SQLite outbox. Dependencies are pinned in `backend/requirements.txt`.
- Frontend: static HTML/CSS and plain browser JavaScript in `frontend/index.html`, `frontend/style.css`, and `frontend/app.js`. No frontend framework or build system is configured.
- Follow the existing four-space Python indentation and snake_case functions; use two-space JavaScript/CSS indentation, camelCase JavaScript functions/variables, and uppercase endpoint constants.
- Keep HTML IDs, JavaScript DOM bindings, and CSS visibility rules aligned. Preserve asynchronous WebSocket sends in backend handlers.
- WebSocket messages use JSON with a `type` discriminator and snake_case payload keys. Check both sender and receiver when changing the protocol; consult the map's known mismatches rather than assuming existing flows work.
- Room lifecycle, question answers, deadlines and scoring are backend-owned. The browser buffers prompts and displays confirmed scores. Never accept browser score totals or UIDs; ranking eligibility is frozen at round start and can only be revoked during a round.

## Verification context

- Run `python -B -m unittest discover -s tests -v`, `node tests/test_frontend_replay.cjs`, `node tests/test_frontend_auth.cjs`, and JavaScript syntax checks. Tests use isolated Firebase fakes; do not run against production Firebase without explicit approval.
- `frontend/config.js` selects localhost:8000 for local pages and the existing Render service otherwise. For safe browser testing use `python -B -m tests.local_server`; it replaces Firebase with in-memory fakes. See docs/FIREBASE_SETUP.md for credentials and deployment settings.
- Keep Firestore client rules deny-all, run one backend worker, and never add service-account secrets to frontend files or Git. Do not install dependencies, commit, push or deploy without user approval.
