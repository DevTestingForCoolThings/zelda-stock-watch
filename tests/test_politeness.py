"""
Request spacing, rate-limit cooldowns, hourly discovery and the local-run
guard. Uses a throwaway HTTP server on this machine; no real store is contacted.
"""

import http.server
import shutil
import threading
import time
import unittest

from helpers import WatcherTestCase, make_cfg, z


class Handler(http.server.BaseHTTPRequestHandler):
    hits = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        if self.path.startswith("/limited"):
            self.send_response(429)
            if self.path == "/limited":
                self.send_header("Retry-After", "120")
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"slow down")
            return
        body = b'<html>"availability":"https://schema.org/OutOfStock"</html>'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PolitenessTest(WatcherTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        Handler.hits.clear()
        self.patch(z, "REQUEST_SPACING_SECONDS", 1.0)
        self.patch(z, "DEFAULT_COOLDOWN_MINUTES", 30)

    def local_cfg(self, path, **over):
        """A config whose one product lives on the local server, as a custom store."""
        return make_cfg([{"name": "Local", "links": [self.base + path]}],
                        custom_stores=[{"id": "local", "domains": ["127.0.0.1"]}],
                        approved_stores=["local"], **over)

    def test_same_host_waits_the_spacing(self):
        t = time.monotonic()
        z.polite_wait("https://a.example/1")
        z.polite_wait("https://a.example/2")
        self.assertGreaterEqual(time.monotonic() - t, 0.95)

    def test_different_hosts_do_not_wait(self):
        t = time.monotonic()
        z.polite_wait("https://a.example/1")
        z.polite_wait("https://b.example/1")
        self.assertLess(time.monotonic() - t, 0.2)

    def test_429_honours_retry_after_then_goes_quiet(self):
        with self.assertRaises(z.RateLimited):
            z.fetch(self.base + "/limited")
        left = z._cooldowns["127.0.0.1"] - time.time()
        self.assertTrue(100 < left <= 121, left)
        with self.assertRaises(z.RateLimited):
            z.fetch(self.base + "/ok")
        self.assertNotIn("/ok", Handler.hits)  # never contacted while cooling down

    def test_429_without_retry_after_uses_the_default(self):
        with self.assertRaises(z.RateLimited):
            z.fetch(self.base + "/limited-noretry")
        left = z._cooldowns["127.0.0.1"] - time.time()
        self.assertTrue(1790 < left <= 1801, left)

    def test_fallback_chain_stops_at_the_first_429(self):
        with self.assertRaises(z.RateLimited):
            z.fetch_with_fallbacks(self.base + "/limited", "__NEXT_DATA__")
        self.assertEqual(Handler.hits["/limited"], 1)

    @unittest.skipUnless(shutil.which("curl"), "curl not installed")
    def test_curl_detects_429_and_still_returns_bodies(self):
        with self.assertRaises(z.RateLimited):
            z.fetch_via_curl(self.base + "/limited-noretry")
        self.assertEqual(Handler.hits["/limited-noretry"], 1)
        z._cooldowns.clear()
        body = z.fetch_via_curl(self.base + "/ok")
        self.assertIn("schema.org", body)
        self.assertNotIn("__HTTP_STATUS__", body)

    def test_a_rate_limited_link_is_paused_not_broken(self):
        cfg = self.local_cfg("/limited")
        first = self.run_main(cfg, {})
        link = first.state["links"][z.link_key(self.base + "/limited")]
        self.assertEqual(first.rc, 0, first.out)
        self.assertEqual(link.get("fails", 0), 0)
        self.assertIn("paused until", link["last_error"])
        self.assertIn("127.0.0.1", first.state["cooldowns"])

        hits = Handler.hits["/limited"]
        z._cooldowns.clear()   # the next run must get the cooldown from state alone
        z._last_request.clear()
        second = self.run_main(cfg, first.state)
        self.assertEqual(Handler.hits["/limited"], hits)
        self.assertEqual(second.state, first.state)   # stable: no commit every run

    def test_self_test_is_skipped_not_failed_while_rate_limited(self):
        cfg = self.local_cfg("/ok", self_test={"urls": [self.base + "/limited-noretry-123456/"]})
        r = self.run_main(cfg, None, argv=["--self-test"])
        self.assertEqual(r.rc, 1)
        self.assertEqual([t for _, t, _ in r.sent], ["SELF-TEST SKIPPED"])

    def test_local_run_guard(self):
        cfg = self.local_cfg("/ok", politeness={"request_spacing_seconds": 0})
        home = {"GITHUB_ACTIONS": ""}
        first = self.run_main(cfg, {}, env=home)
        self.assertEqual(first.rc, 0, first.out)
        self.assertEqual(self.run_main(cfg, None, env=home, keep_dir=first.dir).rc, 3)
        self.assertEqual(self.run_main(cfg, None, argv=["--force"], env=home, keep_dir=first.dir).rc, 0)
        self.assertEqual(self.run_main(cfg, None, keep_dir=first.dir).rc, 0)   # never on GitHub

    def test_discovery_runs_at_most_hourly(self):
        state = {}
        self.assertFalse(z.discovery_due({"enabled": False}, state))
        self.assertEqual(state, {})
        self.assertTrue(z.discovery_due({"enabled": True}, state))
        self.assertFalse(z.discovery_due({"enabled": True, "every_minutes": 60}, state))
        state["last_discovery"] = "2000-01-01T00:00:00+00:00"
        self.assertTrue(z.discovery_due({"enabled": True, "every_minutes": 60}, state))

    def test_settings_come_from_config(self):
        z.apply_politeness({"politeness": {"request_spacing_seconds": 7, "default_cooldown_minutes": 45}})
        self.assertEqual((z.REQUEST_SPACING_SECONDS, z.DEFAULT_COOLDOWN_MINUTES), (7, 45))


if __name__ == "__main__":
    unittest.main()
