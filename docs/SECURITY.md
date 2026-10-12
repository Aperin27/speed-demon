# Backend security controls

These controls run in the existing FastAPI single worker. No Redis, paid infrastructure, cloud changes or frontend redesign are required. The scoring version, +1/−1 rules, 32-question buffer and permanent username transactions are unchanged.

## Request limits

`backend/security.py` implements token buckets. Each bucket starts with the stated capacity and refills continuously at capacity/seconds; these are burst capacities and sustained refill rates, not fixed-window totals. Every applicable socket/IP/process/account bucket must permit an action. Rejected requests do not partially charge other buckets. IP limits allow a shared household to use several players; socket and verified-UID limits prevent one player from spending the entire allowance.

| Action / policy prefix | Per socket | Per IP | Per process | Per verified UID |
| --- | --- | --- | --- | --- |
| WebSocket attempts (`CONNECT`) | — | 60 / 60s | 300 / 60s | — |
| Create room (`CREATE`) | 6 / 60s | 30 / 300s | 120 / 60s | — |
| Join attempts (`JOIN`) | 20 / 60s | 120 / 60s | 600 / 60s | — |
| Authenticate / refresh profile (`IDENTITY`) | 12 / 60s | 120 / 60s | 300 / 60s | — |
| Start attempts (`START`) | 6 / 60s | 30 / 60s | 120 / 60s | — |
| All non-answer socket controls (`CONTROL`) | 60 / 60s | — | — | — |
| Protocol errors (`ERROR`) | 12 / 30s | 120 / 60s | — | — |
| All HTTP requests except preflight (`HTTP`) | — | 600 / 60s | 3000 / 60s | — |
| Leaderboard requests (`LEADERBOARD`) | — | 120 / 60s | 600 / 60s | — |
| HTTP profile requests (`PROFILE`) | — | 60 / 60s | 300 / 60s | — |
| Username registration (`REGISTER`) | — | 30 / 300s | 60 / 60s | 5 / 300s |
| Valid maths answers | **No rate quota** | **No rate quota** | **No rate quota** | — |

For any cell, use `SD_RATE_{PREFIX}_{SOCKET|IP|GLOBAL|UID}_LIMIT` and `SD_RATE_{PREFIX}_{SOCKET|IP|GLOBAL|UID}_SECONDS`. For example, `SD_RATE_JOIN_IP_LIMIT=240` and `SD_RATE_JOIN_IP_SECONDS=60`. Only policies in the table exist. Positive integers are required; invalid configuration fails startup. `.env.example` is reference material, not automatically loaded.

Reservation availability has no standalone public HTTP endpoint. Its Firestore reads are protected by create/join/identity/start limits. Invalid or missing target rooms and obvious capacity failures are rejected before Firebase reads. HTTP username syntax is checked before token verification. Verified same-name retries return the canonical spelling without opening another registration transaction; different-name retries fail, and initial registration still uses the atomic profile/reservation transaction. The UID bucket is applied after token verification, so unverified UID fields cannot choose another account's allowance.

HTTP throttling returns 429 with a generic `detail`, `Retry-After` and no-store headers. Socket action errors include `retry_after` while retaining the existing error envelope. Once the protocol-error budget is exhausted, the socket closes with code 1008. Valid wrong answers remain legitimate answers and do not consume error quotas. A rate-limited replacement authentication attempt clears the old identity and revokes active-round eligibility rather than retaining a stale identity.

## Resource and validation bounds

| Setting | Default | Behavior |
| --- | --- | --- |
| `SD_MAX_RATE_KEYS` | 20000 | Bounded total bucket keys; expired counters are pruned. Full stores reject new identities instead of evicting live counters. |
| `SD_MAX_CONNECTIONS` | 2000 | Concurrent connections, including handshakes awaiting acceptance. |
| `SD_MAX_CONNECTIONS_PER_IP` | 32 | Shared-IP concurrent cap; reserved before the first handshake await and released on every exit. |
| `SD_MAX_ROOMS` | 1000 | Live room cap. |
| `SD_MAX_ROUNDS` | 1000 | Active deadline-task cap, including rounds whose players departed. |
| `SD_WS_MESSAGE_BYTES` | 16384 | Incoming text/binary byte limit; oversized messages close with 1009. Match the Uvicorn transport limit. |
| `SD_HTTP_BODY_BYTES` | 4096 | Registration body cap, enforced against declared size and actual streamed bytes. |
| `SD_HTTP_BODY_SECONDS` | 10 | Total registration-body receipt timeout; slow bodies return 408. |
| `SD_UNJOINED_SECONDS` | 120 | Maximum time outside a room, initially or after leaving; chatter cannot extend it. |
| `SD_SOCKET_IDLE_SECONDS` | 900 | Maximum wait for the next frame on an admitted connection. |
| `SD_ROOM_IDLE_SECONDS` | 900 | Unused lobby/results expiration since create/join/depart/round completion/replay. Auth chatter does not renew a room. |
| `SD_MAINTENANCE_SECONDS` | 30 | Room-expiration and limiter-pruning cadence. Expiration can lag by this interval. |

The existing eight-player room maximum, three-second send timeout, four-slot Firebase verification concurrency and two-second token-authentication cooldown remain. All configurable bounds require positive integers. Active rounds are exempt from room expiration. Expiration sends a generic notice, closes members with 1001 and removes the room; ordinary final departures remove empty rooms immediately. In-flight admissions cannot recreate rooms on connections being expired, and membership/identity/state changes during start validation cancel that start.

WebSocket messages must be JSON text objects with a known string `type`, at most ten top-level fields, bounded string identifiers and correctly typed answer identifiers/numbers. Unknown extra fields remain ignored for compatibility and confer no authority. Duplicate JSON keys, NaN/Infinity, excessive nesting (eight container levels), malformed JSON and binary protocol messages are rejected. Answer values must be actual finite numbers (not booleans/strings) with absolute value at most 1,000,000. Ordered prompt IDs/indexes, scheduled start/deadline and room membership remain enforced before any score mutation. Valid answers bypass all action quotas; malformed, duplicate, stale, unauthorized and late submissions consume error budgets.

Origins must appear exactly once and exactly match `ALLOWED_ORIGINS`; missing, null, deceptive-suffix and duplicate origins are rejected. An allowed Origin is a browser boundary, not proof of identity: scripts can forge it. Google identity still requires Admin SDK signature/project/provider/expiry verification with `check_revoked=True`. Server player IDs, verified UID and frozen round identity remain authoritative. Host-only start/replay, duplicate account/name rejection, case-insensitive username reservations and guest display separation are retained. Checked-in client Firestore rules remain deny-all; the API explicitly projects leaderboard entries to public names/scores only. Deployed rules/IAM are not verified by local tests.

## Trusted proxies and manual hosting configuration

Run Uvicorn with **`--no-proxy-headers`** so this resolver receives the physical peer. Also pass **`--forwarded-allow-ips ""`** to override Render Python's documented `FORWARDED_ALLOW_IPS=*` default with an empty Uvicorn trust list. The application, not Uvicorn, resolves rate-limit identities. No arbitrary public CIDR is trusted.

### Direct/local deployments and explicitly known proxies

`SD_CLIENT_IP_MODE=proxy-cidrs` is the default. `SD_TRUSTED_PROXY_CIDRS` defaults to empty: forwarded headers are ignored and the transport IP is used. For a verified proxy, configure only its actual ingress addresses/networks. The resolver parses one bounded XFF header from right to left, skips trusted hops, and uses the first untrusted IP. Malformed/duplicate/excessive chains fall back to the peer; IPv4-mapped IPv6 normalizes consistently. CF headers do not override this mode.

No exact Render inbound-proxy CIDRs were verified from the authoritative material reviewed. Render's [outbound ranges](https://render.com/docs/outbound-ip-addresses) describe service egress, not ingress proxies. [Inbound IP rules](https://render.com/docs/inbound-ip-rules) describe allowed client sources, not a proxy address inventory. Do not copy either into the trust list, assume private IPs are trusted, or use all-address ranges. Leaving the trust list empty is a safe anti-spoofing fallback, but public users then share the Render proxy's quotas and 32-connection cap; it is unsuitable for general release with the current defaults. Increasing those caps does not restore per-client protection.

### Explicit Render public-service fallback

For the documented public Render web-service topology, set:

```text
SD_CLIENT_IP_MODE=render-cloudflare
SD_TRUSTED_PROXY_CIDRS=
```

This opt-in mode requires Render's automatic `RENDER=true` and `RENDER_SERVICE_TYPE=web` environment values, otherwise startup fails. Do not set those values manually to enable it locally. Both HTTP and WebSocket paths use the same resolver. It accepts exactly one bounded, syntactically valid `CF-Connecting-IP` IPv4/IPv6 value, ignoring XFF entirely. Missing, malformed or duplicate values fall back to the physical peer, with a throttled generic diagnostic; they never cause permissive XFF fallback. Requests whose peer was already replaced by Uvicorn (port zero plus XFF) remain grouped as `unverified-proxy`, rather than trusting metadata whose provenance was lost.

Render's [public-service guidance](https://render.com/articles/host-pocketbase-on-render) states that Cloudflare overwrites caller-supplied `CF-Connecting-IP`, while XFF retains caller-controlled prefixes. Its [web-service documentation](https://render.com/docs/web-services) states the bound application port is not directly reachable from the public internet. This is a platform ingress trust boundary, not a claim that arbitrary CF headers are authentic. Public callers hitting the onrender.com/custom-domain ingress cannot select the edge-supplied value. Households share a genuine client IP as intended; separate client IPs no longer share one Render proxy bucket. The 32/IP cap permits an eight-player household room; valid answer submissions still have no rate quota.

**Private-network limitation:** services in the same Render workspace/region can reach the application internally, bypassing Cloudflare. Such internal callers or local processes could forge CF headers. Enable this mode only when those callers are trusted, and do not use it for private services, self-hosted public servers, or deployments accepting untrusted direct/private traffic. Headerless internal/health requests use the peer quota. If the ingress/private-network trust boundary cannot be confirmed, retain `proxy-cidrs` with an empty list and mark public release BLOCKED until Render confirms a trustworthy ingress contract or another reviewed ingress arrangement is available. No paid infrastructure is required or enabled by this change.

The owner should verify the existing service's runtime/start settings before the approved release. `backend/render.yaml` is empty; checked-in documentation does not prove live Dashboard settings. No production service was inspected or changed. For the documented public-service mode, use this complete command from the repository root:

```text
uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1 --ws-max-size 16384 --no-proxy-headers --forwarded-allow-ips "" --no-access-log --log-level info
```

Render supplies `PORT` (default 10000). Keep one worker. Retain the existing Firebase project/secret-file/origin/outbox settings in `FIREBASE_SETUP.md`; no new credentials or Firebase changes are required. Mode names, runtime flags and the Python forwarding default are documented in [Render's environment reference](https://render.com/docs/environment-variables). Before public traffic is enabled, check HTTP and WebSocket ingress from two client IPs, including deliberately forged XFF/CF headers, to confirm the edge replaces the supplied CF value. Local isolated tests simulate this contract; they do not prove its live enforcement.

`--no-access-log` disables HTTP access/query logging. Never send credentials in URLs or enable frame/header debug or production payload logging: authentication travels in headers/private frames. Uvicorn's informational WebSocket connection logs can still contain the requested path. Operational diagnostics log fixed failure categories only, without tokens, UIDs, keys, email addresses, bodies or exception text, and suppress repeats per category for 60 seconds. Queue retries still occur normally. No log is emitted for each ordinary limiter rejection. The local fake server disables proxy parsing/access logs and retains direct/local IP mode.

## Verification and remaining limits

Run the existing full Python and Node regression commands in `REPO_MAP.md`; `tests/test_security.py` adds simulated token-bucket recovery, bounded storage, proxy spoofing, HTTP/UID throttles, body receipt/size errors, concurrent handshake caps, protocol errors, expiration/cleanup and 256 ordered correct/wrong answers in one limiter-clock instant with control/error quotas exhausted. Existing tests continue covering guest/signed-in play, host authority, replay, disconnects, permanent registration concurrency, scoring/board integrity and four-duration buffering. Use only `tests.local_server` for real local browser checks; its Firebase database/token verifier are isolated fakes.

Counters reset on restart, cannot coordinate multiple workers/instances, and protect application work rather than upstream bandwidth or the TLS handshake. Shared NATs may need reviewed threshold adjustments. Distributed IPs/accounts, valid automated solving, sustained valid-answer CPU floods and attacks above the hosting layer remain risks. There is deliberately no speed quota for legitimate answers. Persistent result-queue storage remains constrained by the existing ephemeral host filesystem.

Possible future controls, only after separate review: [Firebase App Check](https://firebase.google.com/docs/app-check) with server-side attestation verification for this custom backend, managed WAF/bot rules at an existing ingress, and external shared rate-limit storage if multi-worker hosting is introduced. App Check supplements authentication and does not prove a human solved the questions. These controls are not enabled here; any vendor quota/billing and guest/shared-IP impact must be reviewed before adoption. No paid service is introduced.
