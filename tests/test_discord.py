"""
Discord delivery: webhook messages, role pings, and the safety rules around
them. No real Discord server is contacted: payloads are captured, and the HTTP
side is exercised against a throwaway local server.
"""

import contextlib
import http.server
import io
import json
import threading
import unittest

from helpers import WatcherTestCase, make_cfg, z
from test_watch import NIN_CA_IN, NIN_CA_OUT, FakeWeb, web_pages

ROLE = "123456789012345678"
HOOK = "https://discord.com/api/webhooks/111/SECRET-WEBHOOK-TOKEN"


def discord_cfg(products, **discord):
    cfg = make_cfg(products)
    settings = {"enabled": True, "topic_roles": {"Zelda 2026": ROLE}}
    settings.update(discord)
    cfg["notifications"]["discord"] = settings
    return cfg


def zelda(**over):
    product = {"name": "Zelda Pro Controller", "topic": "Zelda 2026", "links": [NIN_CA_IN]}
    product.update(over)
    return product


class PayloadTest(unittest.TestCase):
    def alert(self, **over):
        a = {"title": "IN STOCK: Zelda Pro Controller", "body": "Nintendo Store: in stock",
             "priority": "max", "tags": "", "click": NIN_CA_IN}
        a.update(over)
        return a

    def test_pings_only_the_topic_role(self):
        p = z.discord_payload(self.alert(), ROLE)
        self.assertTrue(p["content"].startswith("<@&%s>" % ROLE))
        self.assertEqual(p["allowed_mentions"], {"parse": [], "roles": [ROLE]})

    def test_no_role_means_no_ping(self):
        p = z.discord_payload(self.alert(), None)
        self.assertNotIn("<@&", p["content"])
        self.assertEqual(p["allowed_mentions"], {"parse": [], "roles": []})

    def test_everyone_in_a_product_name_cannot_ping_anyone(self):
        p = z.discord_payload(self.alert(title="IN STOCK: @everyone @here <@&999999999999999999>"), None)
        self.assertEqual(p["allowed_mentions"], {"parse": [], "roles": []})

    def test_long_messages_fit_discord_limits(self):
        p = z.discord_payload(self.alert(title="t" * 500, body="x" * 10000), ROLE)
        self.assertLessEqual(len(p["content"]), 2000)
        self.assertLessEqual(len(p["embeds"][0]["title"]), 256)
        self.assertLessEqual(len(p["embeds"][0]["description"]), 4096)

    def test_link_is_clickable_when_there_is_one(self):
        self.assertEqual(z.discord_payload(self.alert())["embeds"][0]["url"], NIN_CA_IN)
        self.assertNotIn("url", z.discord_payload(self.alert(click=""))["embeds"][0])


class DeliveryTest(WatcherTestCase):
    def setUp(self):
        super().setUp()
        self.web = FakeWeb(web_pages())
        self.patch(z, "fetch", self.web.fetch)
        self.patch(z, "fetch_via_curl", lambda url, *a, **k: self.web.fetch(url))
        self.patch(z, "REQUEST_SPACING_SECONDS", 0)
        self.posts = []
        self.patch(z, "post_discord", lambda hook, payload: self.posts.append((hook, payload)) or True)

    def run_d(self, cfg, state=None, argv=(), hook=HOOK, **env):
        env = dict({"DISCORD_WEBHOOK_URL": hook}, **env)
        return self.run_main(cfg, {} if state is None else state, argv=argv, env=env)

    def test_restock_goes_to_ntfy_and_discord_with_a_role_ping(self):
        r = self.run_d(discord_cfg([zelda()]))
        self.assertEqual([t for _, t, _ in r.sent], ["IN STOCK: Zelda Pro Controller"], r.out)
        self.assertEqual(len(self.posts), 1, r.out)
        hook, payload = self.posts[0]
        self.assertEqual(hook, HOOK)
        self.assertIn("<@&%s>" % ROLE, payload["content"])
        self.assertEqual(payload["allowed_mentions"]["roles"], [ROLE])

    def test_a_product_can_stay_off_discord(self):
        r = self.run_d(discord_cfg([zelda(discord=False)]))
        self.assertEqual(len(r.sent), 1)
        self.assertEqual(self.posts, [])

    def test_topic_without_a_role_posts_without_a_ping(self):
        r = self.run_d(discord_cfg([zelda(topic="Something else")]))
        self.assertEqual(len(self.posts), 1, r.out)
        self.assertNotIn("<@&", self.posts[0][1]["content"])

    def test_status_alerts_stay_off_discord_unless_asked(self):
        cfg = discord_cfg([zelda(links=[NIN_CA_OUT])])
        cfg["heartbeat_hours"] = 6
        r = self.run_d(cfg)
        self.assertEqual([t for _, t, _ in r.sent], ["Watcher still alive"])
        self.assertEqual(self.posts, [])
        cfg["notifications"]["discord"]["status_alerts"] = True
        self.run_d(cfg)
        self.assertEqual([p["embeds"][0]["title"] for _, p in self.posts], ["Watcher still alive"])

    def test_self_test_posts_to_discord_too(self):
        cfg = discord_cfg([zelda(links=[NIN_CA_OUT])], )
        cfg["self_test"] = {"urls": [NIN_CA_IN]}
        r = self.run_d(cfg, argv=["--self-test"])
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0][1]["embeds"][0]["title"].startswith("SELF-TEST IN STOCK"))

    def test_missing_webhook_secret_still_sends_ntfy_and_says_so(self):
        cfg = discord_cfg([zelda()])
        cfg["heartbeat_hours"] = 6
        r = self.run_d(cfg, hook="")
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(self.posts, [])
        self.assertIn("DISCORD_WEBHOOK_URL", r.out)
        heartbeat = [b for _, t, b in r.sent if t == "Watcher still alive"][0]
        self.assertIn("DISCORD_WEBHOOK_URL secret is missing", heartbeat)

    def test_a_link_that_is_not_a_discord_webhook_is_refused(self):
        r = self.run_d(discord_cfg([zelda()]), hook="https://example.com/not-discord")
        self.assertEqual(self.posts, [])
        self.assertIn("not a Discord webhook link", r.out)

    def test_discord_alone_needs_no_ntfy_topic(self):
        r = self.run_d(discord_cfg([zelda()]), NTFY_TOPIC="")
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(r.sent, [])
        self.assertEqual(len(self.posts), 1)

    def test_dry_run_shows_what_would_go_to_discord(self):
        r = self.run_d(discord_cfg([zelda()]), DRY_RUN="1")
        self.assertEqual(self.posts, [])
        self.assertIn("+ Discord (ping Zelda 2026)", r.out)

    def test_webhook_link_never_appears_in_logs(self):
        r = self.run_d(discord_cfg([zelda()]), DRY_RUN="1")
        r2 = self.run_d(discord_cfg([zelda()]))
        self.assertNotIn("SECRET-WEBHOOK-TOKEN", r.out + r2.out)

    def test_check_config_mentions_discord_and_topics(self):
        r = self.run_d(discord_cfg([zelda()]), argv=["--check-config"])
        self.assertIn("Discord: on", r.out)
        self.assertIn("topic: Zelda 2026", r.out)


class DiscordConfigTest(unittest.TestCase):
    def test_role_ids_must_be_discord_ids(self):
        cfg = discord_cfg([zelda()], topic_roles={"Zelda 2026": "@Zelda 2026"})
        errors, _ = z.validate_config(cfg)
        self.assertTrue(any("Copy Role ID" in e for e in errors), errors)

    def test_a_topic_without_a_role_is_a_warning_when_discord_is_on(self):
        errors, warnings = z.validate_config(discord_cfg([zelda(topic="New thing")]))
        self.assertEqual(errors, [])
        self.assertTrue(any("'New thing'" in w for w in warnings), warnings)

    def test_topics_do_not_warn_while_discord_is_off(self):
        cfg = discord_cfg([zelda(topic="New thing")], enabled=False)
        self.assertEqual(z.validate_config(cfg), ([], []))


class Hook(http.server.BaseHTTPRequestHandler):
    received = []
    limited_once = set()

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Hook.received.append((self.path, dict(self.headers), body))
        if self.path.startswith("/limited") and self.path not in Hook.limited_once:
            Hook.limited_once.add(self.path)
            payload = b'{"retry_after": 0.1}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404 if self.path.startswith("/gone") else 204)
        self.end_headers()


class WebhookHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Hook.received.clear()
        Hook.limited_once.clear()

    def test_posts_json_with_a_real_user_agent(self):
        payload = z.discord_payload({"title": "IN STOCK: X", "body": "b", "priority": "max",
                                     "tags": "", "click": NIN_CA_IN}, ROLE)
        self.assertTrue(z.post_discord(self.base + "/ok", payload))
        path, headers, body = Hook.received[0]
        self.assertEqual(json.loads(body), payload)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertFalse(headers.get("User-Agent", "").startswith("Python-urllib"))

    def test_waits_out_a_short_rate_limit_once(self):
        self.assertTrue(z.post_discord(self.base + "/limited", {"content": "x"}))
        self.assertEqual(len(Hook.received), 2)

    def test_failure_is_logged_without_the_webhook_link(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = z.post_discord(self.base + "/gone/SECRET-TOKEN", {"content": "x"})
        self.assertFalse(ok)
        self.assertIn("HTTP 404", out.getvalue())
        self.assertNotIn("SECRET-TOKEN", out.getvalue())


if __name__ == "__main__":
    unittest.main()
