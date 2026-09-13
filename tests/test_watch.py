"""
Offline tests for the watcher. Every store is exercised against minimised
copies of real pages in tests/fixtures, so no test contacts a real store.

Each checker is proven on a page known to be in stock *and* one known to be
sold out. Showing only that sold-out pages read as sold out proves nothing,
and that gap is how a real restock was missed on 2026-09-10.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import json
import unittest
import urllib.error

from helpers import WatcherTestCase, fixture, make_cfg, repo_json, z

N = "https://www.nintendo.com/{}/store/products/{}/"
NIN_CA_IN = N.format("en-ca", "nintendo-switch-2-pro-controller-123674")
NIN_CA_OUT = N.format("en-ca", "nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition-127074")
NIN_CA_STAND = N.format("en-ca", "nintendo-switch-2-pro-controller-display-stand-the-legend-of-zelda-40th-anniversary-edition-127076")
NIN_CA_PRE = N.format("en-ca", "nintendo-switch-2-carrying-case-screen-protector-the-legend-of-zelda-40th-anniversary-edition-127073")
NIN_CA_CONSOLE = N.format("en-ca", "nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642")
NIN_US_IN = N.format("us", "nintendo-switch-2-pro-controller-123674")
NIN_US_OUT = N.format("us", "nintendo-switch-2-pro-controller-display-stand-the-legend-of-zelda-40th-anniversary-edition-127076")
BB_API = "https://www.bestbuy.ca/ecomm-api/availability/products?accept-language=en-CA&skus={}"
BB_IN = "https://www.bestbuy.ca/en-ca/product/nintendo-switch-2-pro-controller/19523671"
BB_OUT = "https://www.bestbuy.ca/en-ca/product/20149830"
AMZ_CA_IN = "https://www.amazon.ca/dp/B0FCYL8GG3"
AMZ_CA_OUT = "https://www.amazon.ca/dp/B0HJ6F8L6V"
AMZ_COM_IN = "https://www.amazon.com/dp/B0G1ZD8289"
WM_CA_IN = "https://www.walmart.ca/en/ip/Nintendo-Switch-2-The-Legend-of-Zelda-40th-Anniversary-Edition/4LPRUXHD1MMQ"
WM_CA_OUT = "https://www.walmart.ca/en/ip/Nintendo-Switch-2-Pro-Controller-The-Legend-of-Zelda-40th-Anniversary-Edition/3CVEAW67GHIT"
WM_COM_OUT = "https://www.walmart.com/ip/Nintendo-Switch-2-Pro-Controller-The-Legend-of-Zelda-40th-Anniversary-Edition/20954470204"
EB_OUT = "https://www.ebgames.ca/shop/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-220621"
SHOP_IN = "https://www.limitedrungames.com/products/r-type-dx-cd-soundtrack-limited-run-games-edition"
SHOP_OUT = "https://www.limitedrungames.com/products/switch-limited-run-300-capcom-arcade-stadium-vol-1-event-exclusive"
LRG_STORE = {"id": "limited-run", "name": "Limited Run Games", "domains": ["limitedrungames.com"]}


def web_pages():
    return {
        NIN_CA_IN: fixture("nintendo_ca_instock.html"),
        NIN_CA_OUT: fixture("nintendo_ca_soldout.html"),
        NIN_CA_PRE: fixture("nintendo_ca_preorder.html"),
        NIN_US_IN: fixture("nintendo_us_instock.html"),
        NIN_US_OUT: fixture("nintendo_us_soldout.html"),
        BB_API.format("19523671"): fixture("bestbuy_ca_instock.json"),
        BB_API.format("20149830"): fixture("bestbuy_ca_soldout.json"),
        AMZ_CA_IN: fixture("amazon_ca_instock.html"),
        AMZ_CA_OUT: fixture("amazon_ca_soldout.html"),
        AMZ_COM_IN: fixture("amazon_com_instock.html"),
        WM_CA_IN: fixture("walmart_ca_instock.html"),
        WM_CA_OUT: fixture("walmart_ca_soldout.html"),
        WM_COM_OUT: fixture("walmart_com_soldout.html"),
        EB_OUT: fixture("ebgames_ca_soldout.html"),
        SHOP_IN + ".js": fixture("shopify_instock.js.json"),
        SHOP_OUT + ".js": fixture("shopify_soldout.js.json"),
    }


class FakeWeb:
    """Serves fixtures by exact URL; anything else is a 404. Records every request."""

    def __init__(self, pages):
        self.pages = dict(pages)
        self.requests = []

    def fetch(self, url, *args, **kwargs):
        self.requests.append(url)
        if url in self.pages:
            return self.pages[url]
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


class OfflineTestCase(WatcherTestCase):
    def setUp(self):
        super().setUp()
        self.web = FakeWeb(web_pages())
        self.patch(z, "fetch", self.web.fetch)
        self.patch(z, "fetch_via_curl", lambda url, *a, **k: self.web.fetch(url))
        self.patch(z, "REQUEST_SPACING_SECONDS", 0)


# --------------------------------------------------------------------------

class StoreDetectionTest(unittest.TestCase):
    def setUp(self):
        self.stores = z.all_stores({"custom_stores": [LRG_STORE]})

    def test_builtin_stores_by_domain(self):
        cases = {NIN_CA_IN: "nintendo", NIN_US_IN: "nintendo", BB_IN: "bestbuy-ca",
                 AMZ_CA_IN: "amazon", AMZ_COM_IN: "amazon", WM_CA_IN: "walmart",
                 WM_COM_OUT: "walmart", EB_OUT: "ebgames"}
        for url, sid in cases.items():
            with self.subTest(url=url):
                self.assertEqual(z.detect_store(url, self.stores), sid)

    def test_custom_store_and_its_subdomains(self):
        self.assertEqual(z.detect_store(SHOP_IN, self.stores), "limited-run")
        self.assertEqual(z.detect_store("https://shop.limitedrungames.com/x", self.stores), "limited-run")

    def test_unknown_and_lookalike_domains(self):
        self.assertIsNone(z.detect_store("https://example.org/p/1", self.stores))
        self.assertIsNone(z.detect_store("https://notnintendo.com/x", self.stores))

    def test_custom_store_cannot_replace_a_builtin(self):
        stores = z.all_stores({"custom_stores": [{"id": "nintendo", "domains": ["x.com"]}]})
        self.assertTrue(stores["nintendo"]["builtin"])

    def test_link_key(self):
        self.assertEqual(z.link_key("HTTPS://WWW.Nintendo.com/us/a/#top"),
                         z.link_key("https://www.nintendo.com/us/a"))
        self.assertNotEqual(z.link_key(SHOP_IN + "?variant=1"), z.link_key(SHOP_IN + "?variant=2"))

    def test_bestbuy_sku_from_either_link_shape(self):
        self.assertEqual(z._bestbuy_sku(BB_IN), "19523671")
        self.assertEqual(z._bestbuy_sku(BB_OUT), "20149830")
        self.assertIsNone(z._bestbuy_sku("https://www.bestbuy.ca/en-ca/category/switch"))


# --------------------------------------------------------------------------

class CheckerTest(OfflineTestCase):
    """Every built-in store, in stock and sold out."""

    def check(self, url, store=None):
        stores = z.all_stores({"custom_stores": [LRG_STORE]})
        store = store or stores[z.detect_store(url, stores)]
        return store["checker"]({"url": url, "store": store})

    def test_nintendo_canada(self):
        self.assertTrue(self.check(NIN_CA_IN)[0])
        self.assertFalse(self.check(NIN_CA_OUT)[0])

    def test_nintendo_us(self):
        self.assertTrue(self.check(NIN_US_IN)[0])
        self.assertFalse(self.check(NIN_US_OUT)[0])

    def test_nintendo_preorder_is_buyable_and_says_so(self):
        buyable, detail = self.check(NIN_CA_PRE)
        self.assertTrue(buyable)
        self.assertIn("pre-order open", detail)

    def test_nintendo_reads_the_product_in_the_link_not_a_related_one(self):
        # Fixtures carry related "decoy" products whose stock is the opposite.
        self.assertGreaterEqual(fixture("nintendo_ca_soldout.html").count('"isSalableQty"'), 3)
        self.assertFalse(self.check(NIN_CA_OUT)[0])
        self.assertTrue(self.check(NIN_CA_IN)[0])

    def test_regression_2026_09_10_find_retailers_does_not_suppress_a_restock(self):
        html = fixture("nintendo_ca_instock.html")
        if "Find retailers" not in html:
            html = html.replace("<body>", "<body><span>Find retailers</span>", 1)
        self.assertNotIn("Add to cart", html)  # rendered client-side, never in server HTML
        self.web.pages[NIN_CA_IN] = html
        old_rule = "InStock" in html and not ("Find retailers" in html and "Add to cart" not in html)
        self.assertFalse(old_rule, "the pre-fix heuristic would have suppressed this")
        self.assertTrue(self.check(NIN_CA_IN)[0])

    def test_nintendo_alerts_when_its_two_signals_disagree(self):
        self.web.pages[NIN_CA_OUT] = fixture("nintendo_ca_soldout.html").replace(
            "schema.org/OutOfStock", "schema.org/InStock")
        buyable, detail = self.check(NIN_CA_OUT)
        self.assertTrue(buyable)
        self.assertIn("disagree", detail)

    def test_bestbuy(self):
        self.assertTrue(self.check(BB_IN)[0])
        self.assertFalse(self.check(BB_OUT)[0])

    def test_bestbuy_link_without_sku_raises(self):
        stores = z.all_stores({})
        with self.assertRaises(ValueError):
            self.check("https://www.bestbuy.ca/en-ca/category/x", stores["bestbuy-ca"])

    def test_amazon_ca_and_com(self):
        self.assertTrue(self.check(AMZ_CA_IN)[0])
        self.assertFalse(self.check(AMZ_CA_OUT)[0])
        self.assertTrue(self.check(AMZ_COM_IN)[0])

    def test_amazon_bot_check_raises_instead_of_reading_sold_out(self):
        self.web.pages[AMZ_CA_IN] = ("<html><form action='/errors/validateCaptcha'>Enter the "
                                     "characters you see below</form></html>")
        with self.assertRaises(ValueError):
            self.check(AMZ_CA_IN)

    def test_walmart_canada(self):
        buyable, detail = self.check(WM_CA_IN)
        self.assertTrue(buyable)
        self.assertIn("sold by Walmart", detail)
        # The sold-out page carries an IN_STOCK recommended product as a decoy.
        self.assertIn('"IN_STOCK"', fixture("walmart_ca_soldout.html"))
        self.assertFalse(self.check(WM_CA_OUT)[0])

    def test_walmart_com(self):
        self.assertFalse(self.check(WM_COM_OUT)[0])

    def test_ebgames(self):
        self.assertFalse(self.check(EB_OUT)[0])


class CustomStoreTest(OfflineTestCase):
    """The reader for shops that are not built in."""

    def generic(self, url, **store):
        return z.check_generic({"url": url, "store": dict({"name": "Example"}, **store)})

    def test_shopify_in_stock_and_sold_out(self):
        buyable, detail = self.generic(SHOP_IN)
        self.assertTrue(buyable)
        self.assertIn("Shopify", detail)
        self.assertFalse(self.generic(SHOP_OUT)[0])

    def test_shopify_specific_variant(self):
        data = json.loads(fixture("shopify_instock.js.json"))
        base = data["variants"][0]
        data["variants"] = [dict(base, id=111, title="Large", available=False),
                            dict(base, id=222, title="Small", available=True)]
        self.web.pages[SHOP_IN + ".js"] = json.dumps(data)
        self.assertFalse(self.generic(SHOP_IN + "?variant=111")[0])
        self.assertTrue(self.generic(SHOP_IN + "?variant=222")[0])
        self.assertTrue(self.generic(SHOP_IN)[0])  # no variant: any size will do

    def test_schema_org_when_not_shopify(self):
        url = "https://shop.example.com/item/42"
        self.web.pages[url] = ('<script type="application/ld+json">{"offers":{"availability":'
                               '"https://schema.org/InStock"}}</script>')
        buyable, detail = self.generic(url)
        self.assertTrue(buyable)
        self.assertIn("schema.org", detail)

    def test_products_link_on_a_non_shopify_shop_falls_through(self):
        url = "https://shop.example.com/products/widget"   # its .js is a 404 here
        self.web.pages[url] = '<script>{"availability": "http://schema.org/OutOfStock"}</script>'
        self.assertFalse(self.generic(url)[0])

    def test_text_markers(self):
        url = "https://shop.example.com/item/7"
        page = ("<html><script>var badge = 'Sold out';</script><body><h1>Widget</h1>"
                "<button>Add to basket</button></body></html>")
        self.web.pages[url] = page
        # "Sold out" is only inside a script, which is not visible text.
        self.assertTrue(self.generic(url, sold_out_text=["Sold out"])[0])
        self.assertTrue(self.generic(url, in_stock_text=["add to basket"])[0])
        self.web.pages[url] = page.replace("Add to basket", "Sold out")
        self.assertFalse(self.generic(url, sold_out_text=["sold out"])[0])
        self.assertFalse(self.generic(url, in_stock_text=["Add to basket"])[0])

    def test_unreadable_page_raises_with_a_hint(self):
        url = "https://shop.example.com/item/9"
        self.web.pages[url] = "<html><body>Widget</body></html>"
        with self.assertRaisesRegex(ValueError, "in_stock_text"):
            self.generic(url)

    def test_bot_check_raises(self):
        url = "https://shop.example.com/item/10"
        self.web.pages[url] = "<html><title>Access Denied</title></html>"
        with self.assertRaisesRegex(ValueError, "bot check"):
            self.generic(url)


# --------------------------------------------------------------------------

class ConfigTest(unittest.TestCase):
    def test_live_config_is_valid(self):
        errors, warnings = z.validate_config(repo_json("config.json"))
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_example_config_is_valid(self):
        errors, warnings = z.validate_config(repo_json("config.example.json"))
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_old_format_is_explained(self):
        errors, _ = z.validate_config({"targets": []})
        self.assertIn('"version": 2', errors[0])

    def test_mistakes_are_explained_in_plain_words(self):
        cfg = make_cfg([{"name": "A", "notify": ["sam"], "links": [NIN_CA_IN]},
                        {"name": "A", "links": []}],
                       approved_stores=["nintendo", "wallmart"])
        text = "\n".join(z.validate_config(cfg)[0])
        self.assertIn("'sam'", text)
        self.assertIn("'wallmart'", text)
        self.assertIn("unique", text)
        self.assertIn("at least one link", text)

    def test_per_link_problems_are_warnings_not_errors(self):
        cfg = make_cfg([{"name": "A", "links": ["https://example.org/p/1", EB_OUT,
                                                "https://www.bestbuy.ca/en-ca/category/x"]}])
        errors, warnings = z.validate_config(cfg)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 3, warnings)

    def test_custom_store_mistakes(self):
        cfg = make_cfg([{"name": "A", "links": [NIN_CA_IN]}], custom_stores=[
            {"id": "My Shop", "domains": ["a.com"]},
            {"id": "nintendo", "domains": ["b.com"]},
            {"id": "shop", "domains": []},
            {"id": "shop2", "domains": ["c.com"], "method": "text"}])
        text = "\n".join(z.validate_config(cfg)[0])
        self.assertIn("lowercase", text)
        self.assertIn("built-in", text)
        self.assertIn("'domains'", text)
        self.assertIn("method 'text'", text)


class WatchlistTest(unittest.TestCase):
    def build(self, cfg, state=None, on_github=True):
        return z.build_watchlist(cfg, state or {}, z.all_stores(cfg), on_github)

    def test_walmart_only_from_a_home_connection(self):
        cfg = make_cfg([{"name": "A", "links": [NIN_CA_IN, WM_CA_IN]}])
        watch, skipped, _ = self.build(cfg, on_github=True)
        self.assertEqual([w["store"]["id"] for w in watch], ["nintendo"])
        self.assertEqual(skipped[0]["reason"], "home connection only")
        watch, skipped, _ = self.build(cfg, on_github=False)
        self.assertEqual(len(watch), 2)
        self.assertEqual(skipped, [])

    def test_unapproved_and_paused_links_are_skipped(self):
        cfg = make_cfg([{"name": "A", "links": [NIN_CA_IN, {"url": AMZ_CA_IN, "enabled": False}, EB_OUT]},
                        {"name": "B", "enabled": False, "links": [BB_IN]}])
        watch, skipped, _ = self.build(cfg)
        self.assertEqual(len(watch), 1)
        self.assertEqual(sorted(s["reason"] for s in skipped), ["not approved", "paused", "paused"])

    def test_auto_watch_adds_each_discovery_once(self):
        cfg = make_cfg([{"name": "Case", "links": [NIN_CA_PRE]}],
                       discovery={"enabled": True, "auto_watch": True})
        watch, _, _ = self.build(cfg, {"auto_watch": [NIN_CA_PRE, NIN_CA_OUT]})
        self.assertEqual(len(watch), 2)
        self.assertTrue(watch[1].get("auto"))


# --------------------------------------------------------------------------

class MigrationTest(OfflineTestCase):
    def test_v1_state_moves_to_links(self):
        cfg = repo_json("config.json")
        state = json.loads(fixture("state_v1.json"))
        old = dict(state["targets"])
        mapping = cfg["migrate_from_v1"]
        moved = z.migrate_state(cfg, state)
        self.assertNotIn("targets", state)
        self.assertEqual(moved, sum(1 for t in old if t in mapping or t.startswith("auto:")))
        for tid, entry in old.items():
            if tid in mapping:
                self.assertEqual(state["links"][z.link_key(mapping[tid])], entry, tid)

    def test_switching_formats_sends_no_repeat_alerts(self):
        cfg = repo_json("config.json")
        cfg.update(heartbeat_hours=0, politeness={"request_spacing_seconds": 0})
        cfg["discovery"]["enabled"] = False
        self.web.pages.update({
            NIN_CA_CONSOLE: fixture("nintendo_ca_soldout.html"),
            NIN_CA_STAND: fixture("nintendo_ca_soldout.html"),
            BB_API.format("20149861"): fixture("bestbuy_ca_soldout.json"),
            BB_API.format("20149880"): fixture("bestbuy_ca_soldout.json"),
        })
        r = self.run_main(cfg, json.loads(fixture("state_v1.json")))
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(r.sent, [], r.out)
        self.assertNotIn("targets", r.state)
        # The carrying case was already on pre-order before the switch.
        self.assertTrue(r.state["links"][z.link_key(NIN_CA_PRE)]["buyable"])


class AlertTest(OfflineTestCase):
    def test_one_push_per_product_across_stores(self):
        r = self.run_main(make_cfg([{"name": "Pro Controller", "links": [NIN_CA_IN, BB_IN, AMZ_CA_IN]}]), {})
        self.assertEqual(len(r.sent), 1, r.out)
        _, title, body = r.sent[0]
        self.assertEqual(title, "IN STOCK: Pro Controller")
        for store in ("Nintendo Store", "Best Buy Canada", "Amazon"):
            self.assertIn(store, body)

    def test_no_repeat_while_still_in_stock(self):
        cfg = make_cfg([{"name": "Pro Controller", "links": [NIN_CA_IN]}])
        first = self.run_main(cfg, {})
        second = self.run_main(cfg, first.state)
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(second.sent, [])

    def test_preorder_title(self):
        r = self.run_main(make_cfg([{"name": "Case", "links": [NIN_CA_PRE]}]), {})
        self.assertEqual(r.sent[0][1], "PRE-ORDER OPEN: Case")

    def discovered(self, urls, products):
        self.patch(z, "discovery_due", lambda disc, state: True)
        self.patch(z, "discover_nintendo_ca", lambda cfg, state: list(urls))
        return make_cfg(products, discovery={"enabled": True, "auto_watch": True,
                                             "notify": ["julie"], "region": "en-ca"})

    def test_new_listing_that_is_sold_out(self):
        cfg = self.discovered([NIN_CA_OUT], [{"name": "Zelda Pro Controller", "links": [NIN_CA_OUT]}])
        r = self.run_main(cfg, {})
        self.assertEqual([t for _, t, _ in r.sent], ["NEW LISTING: Zelda Pro Controller"], r.out)
        self.assertIn("sold out", r.sent[0][2])

    def test_new_listing_already_buyable_is_one_push(self):
        # 2026-09-11: the carrying case was newly listed *and* on pre-order in the
        # same run, and arrived as two separate pushes.
        cfg = self.discovered([NIN_CA_PRE], [{"name": "Carrying case", "links": [NIN_CA_PRE]}])
        r = self.run_main(cfg, {})
        self.assertEqual(len(r.sent), 1, r.out)
        self.assertEqual(r.sent[0][1], "PRE-ORDER OPEN: Carrying case")
        self.assertIn("Just listed", r.sent[0][2])

    def test_unconfigured_discovery_is_auto_watched(self):
        cfg = self.discovered([NIN_CA_OUT], [{"name": "Other", "links": [NIN_CA_IN]}])
        r = self.run_main(cfg, {"links": {z.link_key(NIN_CA_IN): {"buyable": True}}})
        titles = [t for _, t, _ in r.sent]
        self.assertEqual(len(titles), 1, r.out)
        self.assertTrue(titles[0].startswith("NEW LISTING: Nintendo Switch 2 Pro Controller"), titles)
        self.assertIn(NIN_CA_OUT, r.state["auto_watch"])

    def test_broken_link_warns_once(self):
        cfg = make_cfg([{"name": "Gone", "links": [N.format("en-ca", "gone-999999")]}])
        state, titles = {}, []
        for _ in range(z.BROKEN_AFTER + 2):
            r = self.run_main(cfg, state)
            state = r.state
            titles += [t for _, t, _ in r.sent]
        self.assertEqual(titles, ["Watcher problem: Gone (Nintendo Store)"])


class RoutingTest(OfflineTestCase):
    TOPICS = json.dumps({"julie": "topic-julie", "sam": "topic-sam"})

    def two_people(self, products, **over):
        cfg = make_cfg(products, **over)
        cfg["notifications"] = {"people": ["julie", "sam"], "status_alerts_to": ["julie"]}
        return cfg

    def test_each_person_gets_only_their_products(self):
        cfg = self.two_people([{"name": "Julie's", "notify": ["julie"], "links": [NIN_CA_IN]},
                               {"name": "Sam's", "notify": ["sam"], "links": [AMZ_CA_IN]},
                               {"name": "Shared", "links": [BB_IN]}])
        r = self.run_main(cfg, {}, env={"NTFY_TOPICS": self.TOPICS, "NTFY_TOPIC": ""})
        self.assertEqual(sorted((t, title) for t, title, _ in r.sent), sorted([
            ("topic-julie", "IN STOCK: Julie's"), ("topic-sam", "IN STOCK: Sam's"),
            ("topic-julie", "IN STOCK: Shared"), ("topic-sam", "IN STOCK: Shared")]), r.out)

    def test_status_alerts_go_only_to_status_people(self):
        cfg = self.two_people([{"name": "X", "links": [NIN_CA_OUT]}], heartbeat_hours=6)
        r = self.run_main(cfg, {}, env={"NTFY_TOPICS": self.TOPICS, "NTFY_TOPIC": ""})
        self.assertEqual([(t, title) for t, title, _ in r.sent], [("topic-julie", "Watcher still alive")])

    def test_a_shared_topic_gets_one_push(self):
        topics = json.dumps({"julie": "same", "sam": "same"})
        r = self.run_main(self.two_people([{"name": "X", "links": [NIN_CA_IN]}]), {},
                          env={"NTFY_TOPICS": topics, "NTFY_TOPIC": ""})
        self.assertEqual(len(r.sent), 1)

    def test_single_topic_for_everyone(self):
        r = self.run_main(self.two_people([{"name": "X", "notify": ["sam"], "links": [NIN_CA_IN]}]), {},
                          env={"NTFY_TOPIC": "one-topic", "NTFY_TOPICS": ""})
        self.assertEqual([t for t, _, _ in r.sent], ["one-topic"])

    def test_bad_or_missing_secrets_stop_the_run(self):
        cfg = make_cfg([{"name": "X", "links": [NIN_CA_IN]}])
        r = self.run_main(cfg, {}, env={"NTFY_TOPICS": "{not json", "NTFY_TOPIC": ""})
        self.assertEqual(r.rc, 2)
        self.assertIn("NTFY_TOPICS", r.out)
        r = self.run_main(cfg, {}, env={"NTFY_TOPICS": "", "NTFY_TOPIC": ""})
        self.assertEqual(r.rc, 2)

    def test_topics_are_never_logged(self):
        topics = json.dumps({"julie": "secret-topic-j", "sam": "secret-topic-s"})
        cfg = self.two_people([{"name": "X", "links": [NIN_CA_IN]}], heartbeat_hours=6)
        r = self.run_main(cfg, {}, env={"NTFY_TOPICS": topics, "NTFY_TOPIC": ""})
        self.assertTrue(r.sent)
        self.assertNotIn("secret-topic", r.out)


class CommandTest(OfflineTestCase):
    def test_check_config_makes_no_requests(self):
        r = self.run_main(repo_json("config.json"), None, argv=["--check-config"])
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(self.web.requests, [])
        self.assertIn("home connection only", r.out)
        self.assertIn("Zelda 40th Pro Controller", r.out)

    def test_check_link_on_a_custom_shopify_store(self):
        cfg = make_cfg([{"name": "X", "links": [NIN_CA_IN]}], custom_stores=[LRG_STORE],
                       approved_stores=["nintendo", "limited-run"])
        r = self.run_main(cfg, None, argv=["--check-link", SHOP_IN])
        self.assertEqual(r.rc, 0, r.out)
        for text in ("Limited Run Games", "BUYABLE", "Shopify"):
            self.assertIn(text, r.out)

    def test_check_link_on_an_unknown_store_explains_how_to_add_it(self):
        r = self.run_main(make_cfg([{"name": "X", "links": [NIN_CA_IN]}]), None,
                          argv=["--check-link", SHOP_OUT])
        self.assertEqual(r.rc, 0, r.out)
        self.assertIn("not a known store", r.out)
        self.assertIn("not buyable", r.out)
        self.assertIn('"limitedrungames.com"', r.out)

    def test_self_test_uses_the_real_alert_path(self):
        cfg = make_cfg([{"name": "X", "links": [NIN_CA_OUT]}], self_test={"urls": [NIN_CA_OUT, NIN_CA_IN]})
        r = self.run_main(cfg, None, argv=["--self-test"])
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(len(r.sent), 1)
        self.assertTrue(r.sent[0][1].startswith("SELF-TEST IN STOCK: Nintendo Switch 2 Pro Controller"))

    def test_self_test_fails_loudly(self):
        cfg = make_cfg([{"name": "X", "links": [NIN_CA_OUT]}], self_test={"urls": [NIN_CA_OUT]})
        r = self.run_main(cfg, None, argv=["--self-test"])
        self.assertEqual(r.rc, 1)
        self.assertEqual(r.sent[0][1], "SELF-TEST FAILED")

    def test_heartbeat_lists_skipped_links_and_the_self_test(self):
        cfg = make_cfg([{"name": "Console", "links": [NIN_CA_OUT, WM_CA_IN]}],
                       heartbeat_hours=6, self_test={"urls": [NIN_CA_IN]})
        r = self.run_main(cfg, {})
        heartbeat = [b for _, t, b in r.sent if t == "Watcher still alive"][0]
        self.assertIn("Walmart x1 (home connection only)", heartbeat)
        self.assertIn("Self-test OK", heartbeat)


if __name__ == "__main__":
    unittest.main()
