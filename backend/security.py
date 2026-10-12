"""Bounded single-worker abuse controls. Valid answers have no rate quota."""
import asyncio
import ipaddress
import json
import logging
import math
import os
import time

from starlette.responses import JSONResponse


def configured_integer(name, default, minimum=1):
    value = int(os.getenv(f"SD_{name}", str(default)))
    if value < minimum:
        raise ValueError(f"SD_{name} must be at least {minimum}")
    return value


DEFAULT_POLICIES = {
    "CONNECT_IP": (60, 60), "CONNECT_GLOBAL": (300, 60),
    "CREATE_SOCKET": (6, 60), "CREATE_IP": (30, 300), "CREATE_GLOBAL": (120, 60),
    "JOIN_SOCKET": (20, 60), "JOIN_IP": (120, 60), "JOIN_GLOBAL": (600, 60),
    "IDENTITY_SOCKET": (12, 60), "IDENTITY_IP": (120, 60), "IDENTITY_GLOBAL": (300, 60),
    "START_SOCKET": (6, 60), "START_IP": (30, 60), "START_GLOBAL": (120, 60),
    "CONTROL_SOCKET": (60, 60),
    "ERROR_SOCKET": (12, 30), "ERROR_IP": (120, 60),
    "HTTP_IP": (600, 60), "HTTP_GLOBAL": (3000, 60),
    "LEADERBOARD_IP": (120, 60), "LEADERBOARD_GLOBAL": (600, 60),
    "PROFILE_IP": (60, 60), "PROFILE_GLOBAL": (300, 60),
    "REGISTER_IP": (30, 300), "REGISTER_GLOBAL": (60, 60), "REGISTER_UID": (5, 300),
}
POLICIES = {name: (configured_integer(f"RATE_{name}_LIMIT", limit), configured_integer(f"RATE_{name}_SECONDS", seconds))
            for name, (limit, seconds) in DEFAULT_POLICIES.items()}
MAX_RATE_KEYS = configured_integer("MAX_RATE_KEYS", 20000)
MAX_CONNECTIONS = configured_integer("MAX_CONNECTIONS", 2000)
MAX_CONNECTIONS_PER_IP = configured_integer("MAX_CONNECTIONS_PER_IP", 32)
MAX_ROOMS = configured_integer("MAX_ROOMS", 1000)
MAX_ROUNDS = configured_integer("MAX_ROUNDS", 1000)
MAX_PLAYERS = 8
WS_MESSAGE_BYTES = configured_integer("WS_MESSAGE_BYTES", 16384)
HTTP_BODY_BYTES = configured_integer("HTTP_BODY_BYTES", 4096)
HTTP_BODY_SECONDS = configured_integer("HTTP_BODY_SECONDS", 10)
SOCKET_IDLE_SECONDS = configured_integer("SOCKET_IDLE_SECONDS", 900)
UNJOINED_SECONDS = configured_integer("UNJOINED_SECONDS", 120)
ROOM_IDLE_SECONDS = configured_integer("ROOM_IDLE_SECONDS", 900)
MAINTENANCE_SECONDS = configured_integer("MAINTENANCE_SECONDS", 30)


def configured_client_ip_mode():
    mode = os.getenv("SD_CLIENT_IP_MODE", "proxy-cidrs")
    if mode not in {"proxy-cidrs", "render-cloudflare"}:
        raise ValueError("SD_CLIENT_IP_MODE must be proxy-cidrs or render-cloudflare")
    if mode == "render-cloudflare" and (os.getenv("RENDER") != "true" or os.getenv("RENDER_SERVICE_TYPE") != "web"):
        raise ValueError("render-cloudflare IP mode requires a Render public web service")
    return mode


CLIENT_IP_MODE = configured_client_ip_mode()
TRUSTED_PROXY_NETWORKS = tuple(ipaddress.ip_network(value.strip(), strict=False)
                               for value in os.getenv("SD_TRUSTED_PROXY_CIDRS", "").split(",") if value.strip())
if any(network.prefixlen == 0 for network in TRUSTED_PROXY_NETWORKS):
    raise ValueError("SD_TRUSTED_PROXY_CIDRS must not trust every address")


class RateLimitError(ValueError):
    def __init__(self, retry_after):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__(f"Too many requests. Try again in {self.retry_after} seconds.")


class RateLimiter:
    def __init__(self, policies=None, max_keys=None, clock=None):
        self.policies = policies if policies is not None else POLICIES
        self.max_keys = max_keys if max_keys is not None else MAX_RATE_KEYS
        self.clock = clock or time.monotonic
        self.buckets = {}
        self.next_prune = 0

    def clear(self):
        self.buckets.clear()
        self.next_prune = 0

    def prune(self, now=None):
        now = self.clock() if now is None else now
        for key, (_, updated) in list(self.buckets.items()):
            if now - updated >= self.policies[key[0]][1]:
                del self.buckets[key]
        self.next_prune = now + 30

    def require(self, *checks):
        # No await: charging a set of buckets is atomic on the single event loop.
        now = self.clock()
        if now >= self.next_prune:
            self.prune(now)
        pending = []
        new_keys = 0
        retry_after = 0
        for policy, identity in checks:
            limit, seconds = self.policies[policy]
            key = (policy, identity)
            tokens, updated = self.buckets.get(key, (float(limit), now))
            tokens = min(limit, tokens + max(0, now - updated) * limit / seconds)
            if tokens < 1:
                retry_after = max(retry_after, (1 - tokens) * seconds / limit)
            if key not in self.buckets:
                new_keys += 1
            pending.append((key, tokens - 1))
        if len(self.buckets) + new_keys > self.max_keys:
            # Never evict live counters: rotating identities must not reset limits.
            retry_after = max(retry_after, 30)
        if retry_after:
            raise RateLimitError(retry_after)
        for key, tokens in pending:
            self.buckets[key] = (tokens, now)


limiter = RateLimiter()
logger = logging.getLogger(__name__)
_last_warning = {}


def operational_failure(category, level=logging.WARNING):
    now = time.monotonic()
    if now - _last_warning.get(category, -math.inf) >= 60:
        _last_warning[category] = now
        logger.log(level, "Operational failure: %s; further messages suppressed for 60 seconds", category)


def client_key(scope):
    peer = scope.get("client")
    if not peer:
        return "unknown"
    headers = scope.get("headers", [])
    forwarded = [value for name, value in headers if name.lower() == b"x-forwarded-for"]
    # Uvicorn proxy middleware discards the physical peer and sets port zero.
    # Group ambiguous peers rather than accept a potentially forged client IP.
    if peer[1] == 0 and forwarded:
        operational_failure("proxy_peer_unavailable")
        return "unverified-proxy"
    try:
        address = ipaddress.ip_address(peer[0])
    except ValueError:
        return "unknown"
    address = getattr(address, "ipv4_mapped", None) or address
    if CLIENT_IP_MODE == "render-cloudflare":
        # Render documents that its public ingress overwrites this header.
        # Private-network callers must be trusted; this is not a generic proxy mode.
        values = [value for name, value in headers if name.lower() == b"cf-connecting-ip"]
        if len(values) == 1 and len(values[0]) <= 64:
            try:
                client = ipaddress.ip_address(values[0].decode("ascii").strip())
                if getattr(client, "scope_id", None) is not None:
                    raise ValueError("Scoped addresses are not ingress client IPs")
                return str(getattr(client, "ipv4_mapped", None) or client)
            except (ValueError, UnicodeError):
                pass
        operational_failure("render_client_ip_unavailable")
        return str(address)
    trusted = lambda ip: any(ip in network for network in TRUSTED_PROXY_NETWORKS)
    if not trusted(address) or len(forwarded) != 1 or len(forwarded[0]) > 1024:
        return str(address)
    try:
        chain = [ipaddress.ip_address(value.strip()) for value in forwarded[0].decode("ascii").split(",")]
        if len(chain) > 10:
            return str(address)
        for hop in reversed(chain):
            hop = getattr(hop, "ipv4_mapped", None) or hop
            if not trusted(hop):
                return str(hop)
    except (ValueError, UnicodeError):
        pass
    return str(address)


def reject_constant(value):
    raise ValueError("Non-finite JSON numbers are unsupported")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields")
        result[key] = value
    return result


def decode_json(raw):
    value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            if depth >= 8:
                raise ValueError("JSON nesting is too deep")
            pending.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return value


MESSAGE_TYPES = {"authenticate", "refresh_profile", "create_room", "join_room", "start_game", "answer", "play_again", "leave_room"}


def validate_message(message):
    if not isinstance(message, dict) or len(message) > 10 or not isinstance(message.get("type"), str) or message["type"] not in MESSAGE_TYPES:
        raise ValueError("Invalid or unsupported protocol message")
    kind = message["type"]
    fields = {"room_code": 6, "round_id": 32, "question_id": 32}
    for field, length in fields.items():
        if field == "room_code" and kind == "create_room" and message.get(field) is None:
            continue
        if field in message and (not isinstance(message[field], str) or not 1 <= len(message[field]) <= length):
            raise ValueError(f"Invalid {field}")
    if kind == "authenticate":
        token = message.get("id_token")
        if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 12000):
            raise ValueError("Invalid authentication token")
    if kind in {"join_room", "start_game", "answer", "play_again", "leave_room"} and "room_code" not in message:
        raise ValueError("Room code required")
    if kind == "answer":
        answer = message.get("answer")
        if type(answer) not in (int, float) or abs(answer) > 1000000 or not math.isfinite(answer):
            raise ValueError("Invalid numeric answer")
        if type(message.get("index")) is not int or message["index"] < 0 or not all(field in message for field in ("round_id", "question_id")):
            raise ValueError("Invalid answer identifiers")


def limit_control(player, kind):
    if kind == "answer":
        return
    ip, socket = player.get("ip", "unknown"), player["id"]
    checks = [("CONTROL_SOCKET", socket)]
    group = {"create_room": "CREATE", "join_room": "JOIN", "authenticate": "IDENTITY", "refresh_profile": "IDENTITY", "start_game": "START"}.get(kind)
    if group:
        checks.extend([(f"{group}_SOCKET", socket), (f"{group}_IP", ip), (f"{group}_GLOBAL", "process")])
    limiter.require(*checks)


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        ip = client_key(scope)
        route = {("GET", "/leaderboard"): "LEADERBOARD", ("GET", "/profile"): "PROFILE", ("POST", "/username"): "REGISTER"}.get((scope["method"], scope["path"]))
        checks = [("HTTP_IP", ip), ("HTTP_GLOBAL", "process")]
        if route:
            checks.extend([(f"{route}_IP", ip), (f"{route}_GLOBAL", "process")])
        try:
            limiter.require(*checks)
        except RateLimitError as error:
            response = JSONResponse({"detail": str(error)}, status_code=429, headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"})
            return await response(scope, receive, send)
        if scope["method"] == "POST" and scope["path"] == "/username":
            lengths = [value for name, value in scope.get("headers", []) if name.lower() == b"content-length"]
            if lengths:
                try:
                    length = int(lengths[0])
                    if len(lengths) != 1 or length < 0:
                        raise ValueError("Invalid length")
                except ValueError:
                    return await JSONResponse({"detail": "Invalid content length."}, status_code=400)(scope, receive, send)
                if length > HTTP_BODY_BYTES:
                    return await JSONResponse({"detail": "Request body too large."}, status_code=413)(scope, receive, send)
            body = bytearray()
            deadline = time.monotonic() + HTTP_BODY_SECONDS
            try:
                while True:
                    event = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
                    if event["type"] == "http.disconnect":
                        return
                    chunk = event.get("body", b"")
                    if len(body) + len(chunk) > HTTP_BODY_BYTES:
                        return await JSONResponse({"detail": "Request body too large."}, status_code=413)(scope, receive, send)
                    body.extend(chunk)
                    if not event.get("more_body", False):
                        break
                payload = decode_json(body)
                if not isinstance(payload, dict) or len(payload) > 10:
                    raise ValueError("Invalid request")
            except asyncio.TimeoutError:
                return await JSONResponse({"detail": "Request body timed out."}, status_code=408)(scope, receive, send)
            except (ValueError, UnicodeError, RecursionError):
                return await JSONResponse({"detail": "Invalid JSON request."}, status_code=400)(scope, receive, send)
            async def replay_body():
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            receive = replay_body
        await self.app(scope, receive, send)
