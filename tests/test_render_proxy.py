import ipaddress
import unittest
from unittest.mock import patch

from backend import security
from tests import test_security as fixtures

request = fixtures.request


class RenderProxyTests(unittest.TestCase):
    def scope(self, headers=(), peer="10.1.0.1", port=54321):
        return {"client": (peer, port), "headers": list(headers)}

    def test_render_mode_requires_explicit_opt_in_and_public_render_runtime(self):
        with patch.dict("os.environ", {"SD_CLIENT_IP_MODE": "proxy-cidrs"}, clear=True):
            self.assertEqual(security.configured_client_ip_mode(), "proxy-cidrs")
        for environment in ({}, {"RENDER": "true"}, {"RENDER": "true", "RENDER_SERVICE_TYPE": "pserv"}):
            with patch.dict("os.environ", {"SD_CLIENT_IP_MODE": "render-cloudflare", **environment}, clear=True):
                with self.assertRaises(ValueError):
                    security.configured_client_ip_mode()
        with patch.dict("os.environ", {"SD_CLIENT_IP_MODE": "render-cloudflare", "RENDER": "true", "RENDER_SERVICE_TYPE": "web"}, clear=True):
            self.assertEqual(security.configured_client_ip_mode(), "render-cloudflare")
        with patch.dict("os.environ", {"SD_CLIENT_IP_MODE": "unknown"}, clear=True):
            with self.assertRaises(ValueError):
                security.configured_client_ip_mode()

    def test_valid_edge_address_ignores_attacker_supplied_xff(self):
        with patch.object(security, "CLIENT_IP_MODE", "render-cloudflare"):
            for xff in (b"192.0.2.99, 203.0.113.4", b"malformed", b"198.51.100.7"):
                scope = self.scope([(b"cf-connecting-ip", b"203.0.113.4"), (b"x-forwarded-for", xff)])
                self.assertEqual(security.client_key(scope), "203.0.113.4")
            self.assertEqual(security.client_key(self.scope([(b"cf-connecting-ip", b"2001:db8::1")])), "2001:db8::1")
            self.assertEqual(security.client_key(self.scope([(b"cf-connecting-ip", b"::ffff:203.0.113.4")])), "203.0.113.4")

    def test_missing_malformed_duplicate_headers_fall_back_to_physical_peer(self):
        bad_headers = [[], [(b"cf-connecting-ip", b"invalid")], [(b"cf-connecting-ip", b"203.0.113.4, 192.0.2.9")],
                       [(b"cf-connecting-ip", b"x" * 65)], [(b"cf-connecting-ip", b"\xff")],
                       [(b"cf-connecting-ip", b"fe80::1%eth0")],
                       [(b"cf-connecting-ip", b"203.0.113.4"), (b"CF-Connecting-IP", b"192.0.2.9")]]
        with patch.object(security, "CLIENT_IP_MODE", "render-cloudflare"):
            for headers in bad_headers:
                with self.subTest(headers=headers):
                    self.assertEqual(security.client_key(self.scope([*headers, (b"x-forwarded-for", b"192.0.2.88")])), "10.1.0.1")

    def test_default_direct_and_localhost_mode_ignores_spoofed_headers(self):
        with patch.object(security, "CLIENT_IP_MODE", "proxy-cidrs"), patch.object(security, "TRUSTED_PROXY_NETWORKS", ()):
            for peer in ("127.0.0.1", "::1", "198.51.100.5"):
                self.assertEqual(security.client_key(self.scope([(b"cf-connecting-ip", b"192.0.2.9"), (b"x-forwarded-for", b"192.0.2.9")], peer=peer)), peer)

    def test_existing_trusted_proxy_mode_and_unknown_peer_guards_remain(self):
        with patch.object(security, "CLIENT_IP_MODE", "proxy-cidrs"), patch.object(security, "TRUSTED_PROXY_NETWORKS", (ipaddress.ip_network("10.1.0.0/16"),)):
            self.assertEqual(security.client_key(self.scope([(b"cf-connecting-ip", b"192.0.2.9"), (b"x-forwarded-for", b"203.0.113.4, 10.1.0.2")])), "203.0.113.4")
        with patch.object(security, "CLIENT_IP_MODE", "render-cloudflare"):
            self.assertEqual(security.client_key(self.scope([(b"cf-connecting-ip", b"203.0.113.4"), (b"x-forwarded-for", b"192.0.2.9")], port=0)), "unverified-proxy")
            self.assertEqual(security.client_key({"headers": [(b"cf-connecting-ip", b"203.0.113.4")]}), "unknown")


class RenderHttpProxyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.HttpSecurityTests.asyncSetUp
    asyncTearDown = fixtures.HttpSecurityTests.asyncTearDown

    async def test_different_households_get_separate_quotas_behind_same_proxy(self):
        self.limiter.policies["LEADERBOARD_IP"] = (1, 60)
        with patch.object(security, "CLIENT_IP_MODE", "render-cloudflare"):
            for address in (b"203.0.113.1", b"203.0.113.2"):
                status, _, _ = await request("/leaderboard", ip="10.1.0.1", headers=[(b"cf-connecting-ip", address)])
                self.assertEqual(status, 200)
            status, _, _ = await request("/leaderboard", ip="10.1.0.1", headers=[(b"cf-connecting-ip", b"203.0.113.1"), (b"x-forwarded-for", b"192.0.2.99")])
            self.assertEqual(status, 429)

    async def test_headerless_internal_requests_use_peer_quota(self):
        self.limiter.policies["LEADERBOARD_IP"] = (1, 60)
        with patch.object(security, "CLIENT_IP_MODE", "render-cloudflare"):
            self.assertEqual((await request("/leaderboard", ip="10.1.0.1"))[0], 200)
            self.assertEqual((await request("/leaderboard", ip="10.1.0.1", headers=[(b"x-forwarded-for", b"192.0.2.9")]))[0], 429)


class RenderSocketProxyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SocketSecurityTests.asyncSetUp
    asyncTearDown = fixtures.SocketSecurityTests.asyncTearDown
    connect = fixtures.SocketSecurityTests.connect

    async def test_households_have_independent_connection_caps_behind_one_proxy(self):
        def client(address):
            socket = fixtures.SecurityClient(ip="10.1.0.1")
            socket.scope["headers"] = [(b"cf-connecting-ip", address)]
            return socket
        with patch.object(security, "CLIENT_IP_MODE", "render-cloudflare"), patch.object(security, "MAX_CONNECTIONS_PER_IP", 1):
            await self.connect(client(b"203.0.113.1"))
            await self.connect(client(b"203.0.113.2"))
            rejected = client(b"203.0.113.1")
            await fixtures.main.websocket_endpoint(rejected)
            self.assertEqual(rejected.closed, 1008)
            self.assertEqual(set(fixtures.main.connection_ips.values()), {"203.0.113.1", "203.0.113.2"})
