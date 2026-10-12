# Project instructions

## Architecture navigation

- Consult `docs/REPO_MAP.md` for unfamiliar architecture, cross-file changes, WebSocket/game-flow work, or startup and verification questions. Small isolated edits can go directly to the relevant source.
- Treat the map as navigation guidance. Verify relevant paths, behavior, and assumptions against actual source before changing anything.
- Update `docs/REPO_MAP.md` when architectural changes invalidate its file responsibilities, endpoints, message protocol, state/data flow, dependencies, or startup/test guidance.

## Stack and conventions

- Backend: Python with FastAPI, Uvicorn, and WebSockets. `backend/main.py` contains the application and in-memory room state; dependencies are declared in `backend/requirements.txt`.
- Frontend: static HTML/CSS and plain browser JavaScript in `frontend/index.html`, `frontend/style.css`, and `frontend/app.js`. No frontend framework or build system is configured.
- Follow the existing four-space Python indentation and snake_case functions; use two-space JavaScript/CSS indentation, camelCase JavaScript functions/variables, and uppercase endpoint constants.
- Keep HTML IDs, JavaScript DOM bindings, and CSS visibility rules aligned. Preserve asynchronous WebSocket sends in backend handlers.
- WebSocket messages use JSON with a `type` discriminator and snake_case payload keys. Check both sender and receiver when changing the protocol; consult the map's known mismatches rather than assuming existing flows work.
- Room lifecycle is backend-owned, while timing and scoring currently run in the browser. Changes across this boundary require checking both implementations.

## Verification context

- No automated test suite or test command is currently configured. Use task-appropriate verification and report what was actually checked.
- The frontend currently targets the deployed Render backend even when served locally. Confirm the intended target before interactive testing; see the map for local startup commands and limitations.
