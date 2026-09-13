#!/usr/bin/env python3
"""
Restock watcher: notify-only stock alerts, pushed to your phone via ntfy.

Watches product links at the stores you approve (Nintendo, Best Buy Canada,
Amazon, Walmart, EB Games, or any shop you add yourself) and sends a push the
moment one becomes buyable. It can also spot new products the first time they
appear on the Nintendo store.

Built to catch the Switch 2 Zelda 40th Anniversary Edition in Canada;
everything specific to that lives in config.json.

Dependencies: none (Python 3 standard library only).
"""

import gzip
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
DIAG_PATH = os.path.join(HERE, "diagnostics.txt")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# How many consecutive failures before we warn you that a source has gone dark.
# A silently broken scraper is the worst failure mode for a stock bot.
BROKEN_AFTER = 6


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print("[{}] {}".format(time.strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# Headers a real Chrome sends. Bot-protection vendors fingerprint the presence,
# order and plausibility of these, not just the User-Agent.
BROWSER_HEADERS = {
    "Accept": HTML_ACCEPT,
    "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# --------------------------------------------------------------------------
# Politeness: per-host spacing and rate-limit cooldowns
#
# Every request goes through polite_wait(), which spaces requests to the same
# host and refuses to contact a host that recently rate-limited us. A 429 (or a
# 503 carrying Retry-After) puts that host on cooldown for as long as it asked,
# or DEFAULT_COOLDOWN_MINUTES if it did not say. Cooldowns are saved in
# state.json so later runs respect them too. Sites that rate-limit an IP tend
# to escalate to longer blocks if the client keeps knocking, so the only safe
# response is to go quiet - not to retry with a different client.
# --------------------------------------------------------------------------

REQUEST_SPACING_SECONDS = 3.0
DEFAULT_COOLDOWN_MINUTES = 30

_last_request = {}   # host -> time.monotonic() of its last request this run
_cooldowns = {}      # host -> unix time before which it must not be contacted


class RateLimited(Exception):
    """A host asked us to slow down, or is still on cooldown from doing so."""


def apply_politeness(cfg):
    """Take spacing and cooldown settings from config.json, if present."""
    global REQUEST_SPACING_SECONDS, DEFAULT_COOLDOWN_MINUTES
    p = cfg.get("politeness") or {}
    REQUEST_SPACING_SECONDS = float(p.get("request_spacing_seconds", REQUEST_SPACING_SECONDS))
    DEFAULT_COOLDOWN_MINUTES = float(p.get("default_cooldown_minutes", DEFAULT_COOLDOWN_MINUTES))


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _paused(host):
    # Deliberately stable across runs (an absolute time, not "N min left"), so
    # a paused link does not rewrite state.json - and commit - every run.
    until = datetime.fromtimestamp(_cooldowns[host], timezone.utc)
    return RateLimited("{} rate-limited us; paused until {} UTC".format(
        host, until.strftime("%Y-%m-%d %H:%M")))


def polite_wait(url):
    """Call before every request: enforces cooldowns and per-host spacing."""
    host = _host(url)
    if _cooldowns.get(host, 0) > time.time():
        raise _paused(host)
    last = _last_request.get(host)
    if last is not None:
        gap = REQUEST_SPACING_SECONDS - (time.monotonic() - last)
        if gap > 0:
            time.sleep(gap)
    _last_request[host] = time.monotonic()


def note_rate_limit(url, retry_after=None, code=429):
    """Put the host on cooldown, then raise RateLimited."""
    seconds = int(DEFAULT_COOLDOWN_MINUTES * 60)
    if retry_after and str(retry_after).strip().isdigit():
        # Honour the server within sane bounds. The HTTP-date form of
        # Retry-After is rare for these sites and falls back to the default.
        seconds = min(max(int(str(retry_after).strip()), 60), 6 * 3600)
    host = _host(url)
    _cooldowns[host] = int(time.time()) + seconds
    log("  !! {} answered HTTP {}; leaving it alone for {} min".format(
        host, code, seconds // 60))
    raise _paused(host)


def fetch(url, timeout=25, accept=HTML_ACCEPT, browser_like=False):
    """GET a URL politely, returning decoded text. Raises on failure."""
    polite_wait(url)
    req = urllib.request.Request(url)
    if browser_like:
        for k, v in BROWSER_HEADERS.items():
            req.add_header(k, v)
    else:
        req.add_header("Accept", accept)
        req.add_header("Accept-Language", "en-CA,en;q=0.9")
        req.add_header("Cache-Control", "no-cache")
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Encoding", "gzip, identity")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    except urllib.error.HTTPError as e:
        retry_after = e.headers.get("Retry-After") if e.headers else None
        if e.code == 429 or (e.code == 503 and retry_after):
            note_rate_limit(url, retry_after, e.code)
        raise
    return raw.decode("utf-8", errors="replace")


def fetch_via_curl(url, timeout=25):
    """
    Fetch using the system curl instead of urllib.

    Some bot-protection vendors fingerprint the TLS handshake (JA3), which no
    amount of HTTP header tuning changes. curl presents a different TLS stack
    from Python's, so it sometimes gets through where urllib does not.
    Returns None if curl is unavailable or fails.
    """
    # -L matters: urllib follows redirects by default and curl does not, so
    # without it curl silently returns an empty redirect body.
    base = ["curl", "-sSL", "--compressed", "--max-time", str(timeout), "-A", UA]
    for k, v in BROWSER_HEADERS.items():
        base += ["-H", "{}: {}".format(k, v)]

    # Prefer HTTP/2 (browsers use it, and the protocol version is itself part
    # of the fingerprint), but not every libcurl build supports the flag.
    for extra in (["--http2"], []):
        polite_wait(url)
        try:
            out = subprocess.run(
                base + extra + ["-w", "\\n__HTTP_STATUS__%{http_code}", url],
                capture_output=True, timeout=timeout + 10)
        except (OSError, subprocess.SubprocessError):
            return None
        body, _, status = out.stdout.rpartition(b"\n__HTTP_STATUS__")
        if status.strip() == b"429":
            note_rate_limit(url, code=429)
        if out.returncode == 0 and body:
            return body.decode("utf-8", errors="replace")
        # Exit code 2 means curl rejected an option; retry without it.
        if out.returncode != 2:
            return None
    return None


BLOCK_MARKERS = (
    r"px-captcha|Robot or human|blocked because we believe|Verify Your Identity"
    r"|Access Denied|Reference #[0-9a-f]|Request unsuccessful"
)


def fetch_with_fallbacks(url, required_marker):
    """
    Fetch a bot-protected page, trying progressively different clients.

    Returns (html, strategy_name). Raises ValueError listing what every
    strategy saw, which is what makes a block diagnosable from CI logs.

    The strategies differ in ways bot vendors actually fingerprint: HTTP
    header set, and TLS stack (urllib vs curl). None of them help against
    IP-reputation blocking, which is the common case from datacenter ranges.
    """
    attempts = [
        ("browser-headers", lambda: fetch(url, browser_like=True)),
        ("plain", lambda: fetch(url)),
        ("curl", lambda: fetch_via_curl(url)),
    ]
    notes = []
    for name, attempt in attempts:
        try:
            candidate = attempt()
        except RateLimited:
            # The host asked us to back off. Trying another client would be
            # ignoring that, so stop here.
            raise
        except Exception as e:
            notes.append("{}: {}".format(name, e))
            continue
        if not candidate:
            notes.append("{}: empty response".format(name))
            continue
        if re.search(BLOCK_MARKERS, candidate, re.I):
            notes.append("{}: bot challenge".format(name))
            continue
        if required_marker not in candidate:
            notes.append("{}: no {} ({}b)".format(name, required_marker, len(candidate)))
            continue
        return candidate, name
    raise ValueError("all fetch strategies failed [{}]".format("; ".join(notes)))


# --------------------------------------------------------------------------
# Built-in stores
#
# Each checker takes a link dict ({"url": ..., plus "store" for custom
# stores}) and returns (buyable: bool, detail: str). Each raises on a fetch or
# parse failure, so the link shows as not working instead of silently reading
# "out of stock" forever.
# --------------------------------------------------------------------------

# schema.org values that mean you can hand over money right now
BUYABLE_SCHEMA = {"InStock", "PreOrder", "BackOrder", "LimitedAvailability", "OnlineOnly"}


def _nintendo_sku(url):
    """Physical Nintendo store items end in a numeric SKU: .../some-name-121642/"""
    m = re.search(r"-(\d{5,})/?$", url)
    return m.group(1) if m else None


def _nintendo_product(html, sku):
    """
    Return the product node for `sku` from the page's __NEXT_DATA__, or None.

    The page carries several product nodes (related items, bundles), so the
    node is matched on the SKU from the URL rather than taken positionally.
    """
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m or not sku:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if str(node.get("sku")) == sku and "isSalableQty" in node:
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def check_nintendo(link):
    """
    Nintendo store product page (any region; server-rendered).

    Primary signal: `isSalableQty` on the product's own __NEXT_DATA__ node,
    which is what drives the Add to cart button. Secondary: schema.org
    availability in the JSON-LD. The two agreed on all 17 items sampled in
    September 2026, in stock and out, in both Canada and the US.

    Deliberately NOT used: the presence of "Find retailers" or "Add to cart"
    text. Add to cart is rendered client-side and never appears in the server
    HTML, and Find retailers is a secondary link shown even on items Nintendo
    sells directly. An earlier version treated "Find retailers without Add to
    cart" as "not sold direct", which suppressed a real InStock reading on
    the Zelda console on 2026-09-10 and missed the restock.
    """
    html = fetch(link["url"])

    m = re.search(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"', html)
    schema = m.group(1) if m else None
    product = _nintendo_product(html, _nintendo_sku(link["url"]))

    if product is None:
        # Games and pages without a numeric SKU have no product node to
        # match; fall back to schema.org alone.
        if schema is None:
            raise ValueError("no availability signal found (page layout changed?)")
        return schema in BUYABLE_SCHEMA, "{} (schema.org only)".format(schema)

    salable = bool(product.get("isSalableQty"))
    schema_buyable = schema in BUYABLE_SCHEMA if schema else None

    price = None
    for k, v in product.items():
        if k.startswith("prices") and isinstance(v, dict):
            price = v.get("finalPrice")
            break

    detail = "isSalableQty={} schema={}".format(salable, schema or "none")
    if price:
        detail += " ${}".format(price)
    # Unreleased items are sold as pre-orders ("Pre-purchase" button). They are
    # genuinely buyable, so they alert, but the push should say what it is.
    if salable and product.get("prePurchase"):
        detail += " - pre-order open, ships {}".format(
            product.get("startShippingDate") or "on release")

    # For a restock bot a missed drop costs far more than a spurious push, so
    # if the two signals ever disagree, alert and say so.
    if schema_buyable is not None and schema_buyable != salable:
        detail += " (signals disagree - alerting to be safe)"
        return True, detail
    return salable, detail


def check_ebgames(link):
    """
    EB Games Canada. Server-rendered and exposes the same schema.org
    availability that Nintendo does.
    """
    html, _ = fetch_with_fallbacks(link["url"], "schema.org")

    m = re.search(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"', html)
    if not m:
        raise ValueError("no schema.org availability found (blocked or layout changed?)")
    status = m.group(1)

    p = re.search(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]{2})?)', html)
    price = "${}".format(p.group(1)) if p else "?"

    buyable = status in BUYABLE_SCHEMA
    return buyable, "{} | {}".format(status, price)


def _bestbuy_sku(url):
    """Best Buy Canada product links end in the SKU: .../product/some-name/19523671"""
    m = re.search(r"/(\d{7,9})(?:[/?#]|$)", url)
    return m.group(1) if m else None


def check_bestbuy(link):
    """Best Buy CA has a clean public availability JSON endpoint."""
    sku = link.get("sku") or _bestbuy_sku(link["url"])
    if not sku:
        raise ValueError("no Best Buy SKU in the link (it should end in the product number)")
    url = (
        "https://www.bestbuy.ca/ecomm-api/availability/products"
        "?accept-language=en-CA&skus=" + urllib.parse.quote(sku)
    )
    data = json.loads(fetch(url, accept="application/json"))
    avail = data.get("availabilities") or []
    if not avail:
        raise ValueError("no availability record for sku " + sku)
    a = avail[0]

    ship = a.get("shipping") or {}
    pick = a.get("pickup") or {}
    buyable = bool(ship.get("purchasable")) or bool(pick.get("purchasable"))
    detail = "shipping={} pickup={}".format(
        ship.get("status", "?"), pick.get("status", "?")
    )
    return buyable, detail


def check_amazon(link):
    """
    Amazon product page (.ca and .com share the layout).

    The reliable signal is the presence of the add-to-cart control together
    with the absence of the #outOfStock box. The visible availability text is
    only used for the human-readable detail line, because parsing it alone
    picks up div attributes rather than the message.
    """
    html = fetch(link["url"])

    blocked = re.search(
        r"(To discuss automated access|Enter the characters you see below"
        r"|/errors/validateCaptcha)", html, re.I)
    if blocked:
        raise ValueError("blocked by Amazon bot check")

    if not re.search(r'id="(add-to-cart-button|outOfStock|availability)"', html):
        raise ValueError("no add-to-cart or outOfStock marker (layout changed?)")

    has_cart = bool(re.search(
        r'(id="add-to-cart-button"|name="submit\.add-to-cart"|id="buy-now-button")', html))
    out_of_stock = bool(re.search(r'id="outOfStock"', html))

    m = re.search(r'primary-availability-message[^>]*>(.*?)</span>', html, re.S)
    if not m:
        m = re.search(r'id="availability".*?<span[^>]*>(.*?)</span>', html, re.S)
    msg = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""

    buyable = has_cart and not out_of_stock
    detail = msg or ("add-to-cart present" if has_cart else "no add-to-cart")
    # Amazon sells unreleased items as pre-orders ("This item will be released
    # on ..."). They are buyable; say so, so the push reads PRE-ORDER OPEN.
    if buyable and re.search(r"will be released|pre-?order", msg, re.I):
        detail = "pre-order open - " + detail
    return buyable, detail[:160]


def _walmart_item_id(url):
    """Walmart product URLs end in the item id: /ip/Some-Name/4LPRUXHD1MMQ"""
    return urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


def check_walmart(link):
    """
    Walmart (.ca and .com share the page structure).

    Reads the main product node out of __NEXT_DATA__ and verifies its
    usItemId matches the URL, so a recommended or related product further
    down the page cannot trigger a false in-stock alert.

    Blocked from GitHub Actions: Walmart sits behind PerimeterX, which blocks
    datacenter IP ranges. It works from a home connection.
    """
    html, _ = fetch_with_fallbacks(link["url"], "__NEXT_DATA__")

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("no __NEXT_DATA__ in page (blocked or layout changed?)")
    try:
        data = json.loads(m.group(1))
        product = data["props"]["pageProps"]["initialData"]["data"]["product"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("could not locate product node: {}".format(e))

    want = _walmart_item_id(link["url"])
    got = product.get("usItemId")
    if got and want and got != want:
        raise ValueError("product node is {} but URL is {}".format(got, want))

    status = product.get("availabilityStatus")
    if not status:
        raise ValueError("product node has no availabilityStatus")

    seller = product.get("sellerName") or "?"
    price_info = product.get("priceInfo") or {}
    current = price_info.get("currentPrice") or {}
    price = current.get("priceString") or "?"

    # A marketplace reseller listing at triple MSRP is not the win we want,
    # so surface who is actually selling it.
    buyable = status == "IN_STOCK"
    detail = "{} | {} | sold by {}".format(status, price, seller)
    return buyable, detail


# --------------------------------------------------------------------------
# Custom stores: any other shop
# --------------------------------------------------------------------------

SCHEMA_AVAILABILITY = re.compile(
    r'"availability"\s*:\s*"(?:https?://schema\.org/)?([A-Za-z]+)"')


def _shopify_product_json(url):
    """
    Shopify shops publish /products/<handle>.js with per-variant availability.
    Returns that JSON, or None if the link is not a Shopify product.
    """
    parts = urllib.parse.urlsplit(url)
    m = re.search(r"/products/([^/?#]+)", parts.path)
    if not m:
        return None
    js = "{}://{}/products/{}.js".format(parts.scheme, parts.netloc, m.group(1))
    try:
        data = json.loads(fetch(js, accept="application/json"))
    except RateLimited:
        raise
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "variants" in data else None


def _shopify_status(data, url):
    variants = data.get("variants") or []
    wanted = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("variant")
    if wanted:
        # A ?variant= link means one specific size or colour.
        v = next((v for v in variants if str(v.get("id")) == wanted[0]), None)
        if v is None:
            raise ValueError("variant {} is not on this product any more".format(wanted[0]))
        buyable = bool(v.get("available"))
        price = v.get("price")
        detail = "Shopify: '{}' {}".format(v.get("title"), "available" if buyable else "sold out")
    else:
        n = sum(1 for v in variants if v.get("available"))
        buyable = bool(data.get("available")) or n > 0
        price = data.get("price")
        detail = "Shopify: {} of {} variant(s) available".format(n, len(variants))
    if isinstance(price, int):
        detail += " ${:.2f}".format(price / 100)
    return buyable, detail


def _schema_status(html):
    found = SCHEMA_AVAILABILITY.findall(html)
    if not found:
        return None
    buyable = [v for v in found if v in BUYABLE_SCHEMA]
    if len(set(found)) == 1:
        return bool(buyable), "schema.org: {}".format(found[0])
    return bool(buyable), "schema.org: {} of {} offers buyable".format(len(buyable), len(found))


def _visible_text(html):
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).lower()


def _text_status(html, store):
    """Stock status from words the shop owner told us to look for."""
    ins = [t.lower() for t in store.get("in_stock_text") or []]
    outs = [t.lower() for t in store.get("sold_out_text") or []]
    if not ins and not outs:
        return None
    text = _visible_text(html)
    hit_in = next((t for t in ins if t in text), None)
    hit_out = next((t for t in outs if t in text), None)
    if ins and outs:
        if hit_in and hit_out:
            return True, "text: found both '{}' and '{}' - alerting to be safe".format(hit_in, hit_out)
        if hit_in:
            return True, "text: found '{}'".format(hit_in)
        if hit_out:
            return False, "text: found '{}'".format(hit_out)
        raise ValueError("none of this store's in_stock_text or sold_out_text is on the page")
    if ins:
        if hit_in:
            return True, "text: found '{}'".format(hit_in)
        return False, "text: '{}' is not on the page".format(ins[0])
    if hit_out:
        return False, "text: found '{}'".format(hit_out)
    return True, "text: '{}' is not on the page".format(outs[0])


def check_generic(link):
    """
    Any shop that is not built in. Tries, in order:

    1. Shopify's product JSON (/products/<handle>.js): precise and aware of
       variants. Many small shops run on Shopify, and their pages often carry
       no other machine-readable stock signal at all.
    2. Words you told it to look for (the store's in_stock_text/sold_out_text).
    3. schema.org availability in the page's structured data (WooCommerce,
       BigCommerce and most large retailers).

    If none of these gives an answer it raises, so the link shows as not
    working instead of silently reading "sold out" forever.
    """
    url = link["url"]
    store = link.get("store") or {}
    method = store.get("method", "auto")
    if method in ("auto", "shopify"):
        data = _shopify_product_json(url)
        if data is not None:
            return _shopify_status(data, url)
        if method == "shopify":
            raise ValueError("not a Shopify product link (no /products/<handle>.js)")
    html = fetch(url, browser_like=True)
    if re.search(BLOCK_MARKERS, html, re.I):
        raise ValueError("the shop served a bot check instead of the product page")
    if method in ("auto", "text"):
        result = _text_status(html, store)
        if result is not None:
            return result
    if method in ("auto", "schema"):
        result = _schema_status(html)
        if result is not None:
            return result
    raise ValueError("could not read stock status: no Shopify data, no schema.org "
                     "availability, and no in_stock_text or sold_out_text for this store")


# github_ok: False means the store blocks GitHub's servers, so its links are
# only checked when the watcher runs somewhere else (a home connection).
STORES = {
    "nintendo": {"name": "Nintendo Store", "domains": ["nintendo.com"],
                 "checker": check_nintendo, "github_ok": True},
    "bestbuy-ca": {"name": "Best Buy Canada", "domains": ["bestbuy.ca"],
                   "checker": check_bestbuy, "github_ok": True},
    "amazon": {"name": "Amazon", "domains": ["amazon.ca", "amazon.com"],
               "checker": check_amazon, "github_ok": True},
    "walmart": {"name": "Walmart", "domains": ["walmart.ca", "walmart.com"],
                "checker": check_walmart, "github_ok": False},
    "ebgames": {"name": "EB Games Canada", "domains": ["ebgames.ca"],
                "checker": check_ebgames, "github_ok": False},
}

# Stores fetched as HTML, and therefore worth probing with --diagnose when a
# block is suspected. Best Buy is excluded: it uses a JSON API, and its HTML
# product pages 403 even from a residential IP. Custom stores are always
# diagnosable.
DIAGNOSABLE = ("ebgames", "walmart", "amazon", "nintendo")


def all_stores(cfg):
    """Built-in stores plus config.json's custom_stores, keyed by id."""
    stores = {sid: dict(s, id=sid, builtin=True) for sid, s in STORES.items()}
    for c in cfg.get("custom_stores") or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        s = {k: v for k, v in c.items() if not k.startswith("_")}
        s.setdefault("name", c["id"])
        s.setdefault("github_ok", True)
        s.update(checker=check_generic, builtin=False)
        stores.setdefault(c["id"], s)   # a custom store cannot replace a built-in
    return stores


def detect_store(url, stores):
    """Store id for a link, by domain (subdomains included), or None."""
    host = _host(url)
    best = None
    for sid, s in stores.items():
        for d in s.get("domains") or []:
            d = str(d).lower().strip().lstrip(".")
            if d and (host == d or host.endswith("." + d)):
                # A custom store beats a built-in on the same domain, and a
                # longer domain beats a shorter one.
                rank = (not s.get("builtin"), len(d))
                if best is None or rank > best[0]:
                    best = (rank, sid)
    return best[1] if best else None


def link_key(url):
    """
    The key a link's state is stored under. Case-insensitive scheme and host,
    no fragment, no trailing slash; the query is kept because on some shops
    it selects the variant (?variant=...).
    """
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


# --------------------------------------------------------------------------
# Nintendo new-product discovery
# --------------------------------------------------------------------------

# Cap on direct page probes per run, so discovery stays polite however many
# foreign-only products happen to match the keyword filters.
MAX_DISCOVERY_PROBES = 15

NINTENDO_PRODUCT_URL = "https://www.nintendo.com/{}/store/products/{}/"


def _product_slug(url):
    m = re.search(r"/store/products/([^/?#<\s]+)", url)
    return m.group(1) if m else None


def discover_nintendo_ca(cfg, state):
    """
    Find matching Nintendo store pages in our region that we have never seen.

    Two inputs, because the Canadian sitemap alone proved unreliable: in
    September 2026 the Zelda 40th Pro Controllers (127074, 127076) and the
    carrying case (127073) were live on en-ca while absent from the CA sitemap,
    though the US sitemap listed them. So every sitemap in `sitemaps` is
    scanned, and a matching product seen only in another region's sitemap is
    probed on our region directly. It counts as discovered once that page
    actually exists with product data.
    """
    d = cfg.get("discovery") or {}
    if not d.get("enabled"):
        return []

    region = d.get("region", "en-ca")
    sitemaps = d.get("sitemaps") or [d["sitemap"]]
    must = [s.lower() for s in d.get("must_match_any", [])]
    also = [s.lower() for s in d.get("and_must_match_any", [])]
    skip = [s.lower() for s in d.get("ignore_containing", [])]

    def wanted(slug):
        s = slug.lower()
        if any(x in s for x in skip):
            return False
        if must and not any(x in s for x in must):
            return False
        return not also or any(x in s for x in also)

    local, foreign = set(), set()
    read = 0
    for sm in sitemaps:
        try:
            xml = fetch(sm, timeout=60, accept="application/xml,text/xml,*/*")
        except Exception as e:
            log("discovery: could not read {} ({})".format(sm, e))
            continue
        read += 1
        for u in re.findall(r"<loc>\s*([^<\s]+/store/products/[^<\s]+)\s*</loc>", xml):
            slug = _product_slug(u)
            if slug and wanted(slug):
                (local if "/{}/".format(region) in u else foreign).add(slug)
    if not read:
        raise ValueError("no sitemap could be read")

    seen = set(state.get("discovered") or [])
    found = {NINTENDO_PRODUCT_URL.format(region, s) for s in local}

    # Products listed only abroad: check whether our region has the page yet.
    candidates = [s for s in sorted(foreign - local)
                  if NINTENDO_PRODUCT_URL.format(region, s) not in seen]
    for slug in candidates[:MAX_DISCOVERY_PROBES]:
        url = NINTENDO_PRODUCT_URL.format(region, slug)
        try:
            html = fetch(url, timeout=20)
        except RateLimited as e:
            log("discovery: {}".format(e))
            break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log("discovery: probe {} -> HTTP {}".format(slug, e.code))
            continue
        except Exception as e:
            log("discovery: probe {} failed ({})".format(slug, e))
            continue
        has_product = (_nintendo_product(html, _nintendo_sku(url)) is not None
                       or "schema.org/" in html)
        if has_product:
            found.add(url)

    # First ever run: record a baseline silently rather than alerting on
    # every product that already exists.
    if not seen:
        state["discovered"] = sorted(found)
        log("discovery: baseline recorded ({} matching products)".format(len(found)))
        return []

    new = sorted(found - seen)
    state["discovered"] = sorted(seen | found)
    log("discovery: {} matching in {} ({} via other regions probed), {} new".format(
        len(found), region, min(len(candidates), MAX_DISCOVERY_PROBES), len(new)))
    return new


# --------------------------------------------------------------------------
# ntfy push
# --------------------------------------------------------------------------

def notify(topic, server, title, body, priority="default", tags="", click=""):
    url = server.rstrip("/") + "/" + topic
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", priority)
    if tags:
        req.add_header("Tags", tags)
    if click:
        req.add_header("Click", click)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log("  -> notified: {}".format(title))
        return True
    except Exception as e:
        log("  !! ntfy failed: {}".format(e))
        return False


def resolve_topics(cfg, environ=None):
    """
    person -> ntfy topic. Topics are secrets and never live in config.json:
    NTFY_TOPICS holds JSON for several people ({"julie": "...", "sam": "..."});
    NTFY_TOPIC is one topic shared by everyone. Topics are never logged.
    """
    environ = os.environ if environ is None else environ
    people = (cfg.get("notifications") or {}).get("people") or []
    raw = (environ.get("NTFY_TOPICS") or "").strip()
    if raw:
        try:
            mapping = json.loads(raw)
        except ValueError:
            mapping = None
        if not isinstance(mapping, dict):
            raise ValueError('the NTFY_TOPICS secret should be JSON like '
                             '{"julie": "topic-one", "sam": "topic-two"}')
        return {p: str(mapping[p]).strip() for p in people if str(mapping.get(p) or "").strip()}
    single = (environ.get("NTFY_TOPIC") or "").strip()
    return {p: single for p in people} if single else {}


# --------------------------------------------------------------------------
# Discord push (optional, alongside or instead of ntfy)
#
# Messages go through a channel webhook, so there is no bot to host. A product
# alert can ping its topic's role, and nothing else: allowed_mentions is locked
# to that one role, so @everyone, @here or a stray mention in a product name
# can never ping the server.
# --------------------------------------------------------------------------

DISCORD_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
)
DISCORD_COLOURS = {"max": 0x2ECC71, "high": 0x5865F2, "low": 0xE67E22}


def discord_payload(alert, role_id=None):
    """A webhook message for one alert, within Discord's size limits."""
    embed = {
        "title": alert["title"][:256],
        "description": alert["body"][:4000],
        "color": DISCORD_COLOURS.get(alert["priority"], 0x95A5A6),
    }
    if alert.get("click"):
        embed["url"] = alert["click"]
    mention = "<@&{}> ".format(role_id) if role_id else ""
    return {
        "username": "Restock Watcher",
        "content": (mention + "**{}**".format(alert["title"]))[:2000],
        "embeds": [embed],
        "allowed_mentions": {"parse": [], "roles": [role_id] if role_id else []},
    }


def post_discord(webhook, payload):
    """
    POST one message to a Discord webhook; True on success. A short rate limit
    is waited out once. Errors are logged by status code only: the webhook
    link is a secret and must never reach the logs.
    """
    data = json.dumps(payload).encode("utf-8")
    for attempt in (1, 2):
        req = urllib.request.Request(webhook, data=data, method="POST", headers={
            "Content-Type": "application/json",
            # Discord's edge rejects some default library User-Agents.
            "User-Agent": "RestockWatcher/2 (+https://github.com)",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                try:
                    wait = float(json.loads(e.read().decode("utf-8") or "{}").get("retry_after", 2))
                except (ValueError, AttributeError):
                    wait = 2.0
                if wait <= 10:
                    time.sleep(wait)
                    continue
            log("  !! Discord refused the message (HTTP {})".format(e.code))
            return False
        except Exception as e:
            log("  !! Discord post failed ({})".format(type(e).__name__))
            return False
    return False


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        # utf-8-sig, not utf-8: editing config.json in Notepad or writing it
        # from PowerShell adds a BOM, which plain utf-8 parsing chokes on.
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        log("warning: could not read {} ({})".format(path, e))
        return default


def _links_of(product):
    """A product's links as dicts, whether written as strings or objects."""
    for item in product.get("links") or []:
        if isinstance(item, str):
            yield {"url": item.strip(), "enabled": True}
        elif isinstance(item, dict):
            yield {"url": str(item.get("url") or "").strip(),
                   "enabled": item.get("enabled", True) is not False}
        else:
            yield {"url": "", "enabled": True, "bad": repr(item)}


def validate_config(cfg):
    """
    Check config.json and explain problems in plain words.

    Returns (errors, warnings). Errors stop the watcher: they are mistakes
    that would otherwise send alerts to the wrong people or silently watch
    nothing. Warnings are per-link problems; those links are skipped and
    listed in the heartbeat, and everything else keeps running.
    """
    errors, warnings = [], []
    if cfg.get("version") != 2:
        errors.append('config.json needs "version": 2 at the top. The first version of this '
                      "watcher used a 'targets' list; see README.md, 'Upgrading from the "
                      "first version'.")
        return errors, warnings

    notif = cfg.get("notifications") or {}
    people = notif.get("people")
    if not isinstance(people, list) or not people or not all(isinstance(p, str) and p for p in people):
        errors.append('notifications.people must list at least one name, e.g. ["me"]')
        people = [p for p in people if isinstance(p, str)] if isinstance(people, list) else []
    known = set(people)

    def check_names(where, names):
        if names is None:
            return
        if not isinstance(names, list):
            errors.append("{} must be a list of names".format(where))
            return
        for n in names:
            if n not in known:
                errors.append("{} mentions '{}', who is not in notifications.people".format(where, n))

    check_names("notifications.status_alerts_to", notif.get("status_alerts_to"))
    check_names("discovery.notify", (cfg.get("discovery") or {}).get("notify"))

    discord = notif.get("discord") or {}
    if not isinstance(discord, dict):
        errors.append("notifications.discord must be an object")
        discord = {}
    roles = discord.get("topic_roles") or {}
    if not isinstance(roles, dict):
        errors.append("notifications.discord.topic_roles must map topic names to role ids")
        roles = {}
    for topic, rid in roles.items():
        if not re.fullmatch(r"\d{17,20}", str(rid)):
            errors.append("the Discord role id for '{}' should be a long number like "
                          "123456789012345678 (turn on Developer Mode, then right-click the "
                          "role and Copy Role ID)".format(topic))

    def check_topic(where, topic):
        if topic is None:
            return
        if not isinstance(topic, str) or not topic.strip():
            errors.append('{}: \'topic\' must be a name like "Zelda 2026"'.format(where))
        elif discord.get("enabled") and topic not in roles:
            warnings.append("{}: topic '{}' has no Discord role in topic_roles, so its Discord "
                            "alerts will not ping anyone".format(where, topic))

    check_topic("discovery", (cfg.get("discovery") or {}).get("topic"))
    check_topic("self_test", (cfg.get("self_test") or {}).get("topic"))

    custom = cfg.get("custom_stores") or []
    if not isinstance(custom, list):
        errors.append("custom_stores must be a list")
        custom = []
    ids = set()
    for i, c in enumerate(custom):
        where = "custom_stores[{}]".format(i)
        if not isinstance(c, dict):
            errors.append(where + " must be an object")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cid):
            errors.append(where + ": 'id' must be lowercase letters, digits and dashes, "
                                  'e.g. "my-shop"')
        elif cid in STORES:
            errors.append('{}: "{}" is already a built-in store; pick another id'.format(where, cid))
        elif cid in ids:
            errors.append('{}: the id "{}" is used twice'.format(where, cid))
        ids.add(cid)
        doms = c.get("domains")
        if not isinstance(doms, list) or not doms or not all(isinstance(d, str) and d for d in doms):
            errors.append(where + ': \'domains\' must list the shop\'s web address, '
                                  'e.g. ["shop.example.com"]')
        method = c.get("method", "auto")
        if method not in ("auto", "shopify", "schema", "text"):
            errors.append(where + ": 'method' must be auto, shopify, schema or text")
        if method == "text" and not (c.get("in_stock_text") or c.get("sold_out_text")):
            errors.append(where + ": method 'text' needs in_stock_text or sold_out_text")

    stores = all_stores(cfg)
    approved = cfg.get("approved_stores")
    if approved is not None:
        if not isinstance(approved, list):
            errors.append("approved_stores must be a list of store ids")
            approved = []
        for a in approved:
            if a not in stores:
                errors.append("approved_stores: '{}' is not a known store. Built in: {}{}".format(
                    a, ", ".join(sorted(STORES)),
                    "; custom: " + ", ".join(sorted(ids)) if ids else ""))

    products = cfg.get("products")
    if not isinstance(products, list) or not products:
        errors.append("products must list at least one product")
        return errors, warnings
    names = set()
    for i, p in enumerate(products):
        where = "products[{}]".format(i)
        if not isinstance(p, dict):
            errors.append(where + " must be an object with a name and links")
            continue
        name = p.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(where + " needs a 'name'")
        else:
            if name in names:
                errors.append("two products are both called '{}'; names must be unique".format(name))
            names.add(name)
            where = "'{}'".format(name)
        check_names(where + " notify", p.get("notify"))
        check_topic(where, p.get("topic"))
        if p.get("discord") not in (None, True, False):
            errors.append(where + ": 'discord' must be true or false")
        links = list(_links_of(p))
        if not links:
            errors.append(where + " needs at least one link")
        for link in links:
            u = link["url"]
            if link.get("bad") or not re.match(r"https?://", u):
                errors.append("{}: {} is not a web link".format(where, link.get("bad") or repr(u)))
                continue
            sid = detect_store(u, stores)
            if sid is None:
                warnings.append("{}: {} is not a built-in store, so that link is skipped. Add it "
                                "under custom_stores (test it first with --check-link).".format(
                                    where, _host(u)))
            elif approved is not None and sid not in approved:
                warnings.append("{}: {} is not in approved_stores, so that link is skipped.".format(
                    where, stores[sid]["name"]))
            elif sid == "bestbuy-ca" and not _bestbuy_sku(u):
                warnings.append("{}: this Best Buy link has no SKU at the end, so it is "
                                "skipped: {}".format(where, u))
    return errors, warnings


def migrate_state(cfg, state):
    """
    Convert version-1 state (keyed by the old target ids) to version 2 (keyed
    by link), so changing formats does not re-send alerts.

    Configured targets map through config.json's migrate_from_v1; auto-watched
    entries ("auto:<slug>") map to their Nintendo URL. Anything unmappable is
    dropped, which costs at most one repeat alert for that link, never a
    missed one. Returns the number of entries moved.
    """
    old = state.pop("targets", None)
    if not old:
        return 0
    links = state.setdefault("links", {})
    mapping = cfg.get("migrate_from_v1") or {}
    region = (cfg.get("discovery") or {}).get("region", "en-ca")
    moved = 0
    for tid, entry in old.items():
        url = (NINTENDO_PRODUCT_URL.format(region, tid[5:]) if tid.startswith("auto:")
               else mapping.get(tid))
        if not url:
            log("migration: no link for old target '{}'; dropping its state".format(tid))
            continue
        links.setdefault(link_key(url), entry)
        moved += 1
    log("migration: moved {} of {} entries to the new format".format(moved, len(old)))
    return moved


def build_watchlist(cfg, state, stores, on_github):
    """
    Expand products (and auto-watched discoveries) into links to check.

    Returns (watch, skipped, configured):
      watch      - link dicts to check this run, grouped by product
      skipped    - links not checked, with the reason, for the heartbeat
      configured - link key -> product name, for every configured link
    """
    notif = cfg.get("notifications") or {}
    people = notif.get("people") or []
    approved = cfg.get("approved_stores")
    watch, skipped, configured = [], [], {}

    def skip(product, url, store, reason):
        skipped.append({"product": product, "url": url, "store": store, "reason": reason})

    for p in cfg.get("products") or []:
        name = p["name"]
        paused = p.get("enabled", True) is False
        for link in _links_of(p):
            key = link_key(link["url"])
            configured[key] = name
            sid = detect_store(link["url"], stores)
            store_name = stores[sid]["name"] if sid else _host(link["url"])
            if paused or not link["enabled"]:
                skip(name, link["url"], store_name, "paused")
            elif sid is None:
                skip(name, link["url"], store_name, "unknown store")
            elif approved is not None and sid not in approved:
                skip(name, link["url"], store_name, "not approved")
            elif on_github and not stores[sid].get("github_ok", True):
                skip(name, link["url"], store_name, "home connection only")
            else:
                watch.append({"key": key, "url": link["url"], "store": stores[sid],
                              "product": name, "notify": p.get("notify") or people,
                              "topic": p.get("topic"),
                              "discord": p.get("discord", True) is not False})

    disc = cfg.get("discovery") or {}
    if disc.get("auto_watch") and (approved is None or "nintendo" in approved):
        for u in state.get("auto_watch", []):
            key = link_key(u)
            if key in configured:
                continue
            watch.append({"key": key, "url": u, "store": stores["nintendo"],
                          "product": _label_from_url(u) + " (auto-watched)",
                          "notify": disc.get("notify") or people, "auto": True,
                          "topic": disc.get("topic"), "discord": True})
    return watch, skipped, configured


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

def stock_alert(name, entries, new_listing=False):
    """
    The in-stock push for one product: every store where it just became
    buyable, in a single notification. The one place it is built, shared by
    real alerts and the self-test, so a passing self-test exercises the exact
    path a real restock takes.

    entries: [(store_name, detail, url), ...]
    """
    preorder = all("pre-order" in detail.lower() for _, detail, _ in entries)
    lines = ["Just listed, and already buyable."] if new_listing else []
    lines += ["{}: {}\n{}".format(store, detail, url) for store, detail, url in entries]
    return {
        "title": ("PRE-ORDER OPEN: " if preorder else "IN STOCK: ") + name,
        "body": "\n\n".join(lines),
        "priority": "max",
        "tags": "rotating_light,shopping_cart",
        "click": entries[0][2],
    }


def listing_alert(name, url, status):
    """A new product page appeared. `status` is None if nothing is watching it."""
    if status is None:
        now = ("It is not being stock-watched. Add it to products, or turn on "
               "discovery.auto_watch.")
    else:
        now = "Right now: {}. You'll get an IN STOCK alert when it can be bought.".format(status)
    return {
        "title": "NEW LISTING: " + name,
        "body": "Just appeared on the Nintendo store.\n\n{}\n\n{}".format(now, url),
        "priority": "high",
        "tags": "sparkles,new",
        "click": url,
    }


def actions_url():
    """Link for pushes about the watcher itself: this repo's Actions tab."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return "https://github.com/{}/actions".format(repo) if repo else ""


def _label_from_url(url):
    slug = re.sub(r"-\d{5,}$", "", _product_slug(url) or url)
    return slug.replace("-", " ").strip().title()


def count_recent_runs(since_iso):
    """
    Count workflow runs since a timestamp, via the public Actions API.

    Turns the heartbeat into a real health report: it says whether the
    schedule is actually firing at the expected rate, which is the one thing
    the watcher cannot otherwise observe about itself. Best-effort - returns
    None if the repo is unknown or the API is unreachable.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo or not since_iso:
        return None
    try:
        # The API's own `created=>DATE` filter returns 0 for valid ranges here,
        # so fetch the recent page and filter client-side instead. 100 runs
        # covers any heartbeat interval up to ~8h at a 5-minute cadence.
        url = ("https://api.github.com/repos/{}/actions/runs"
               "?per_page=100".format(repo))
        data = json.loads(fetch(url, timeout=20, accept="application/vnd.github+json"))
        since = datetime.fromisoformat(since_iso)
        return sum(
            1 for r in data.get("workflow_runs", [])
            if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) > since
        )
    except Exception:
        return None


def self_test(cfg):
    """
    Run the real Nintendo checker against items configured as normally in stock.

    Returns (passed, label, detail, url) for the first item detected as
    buyable; (None, None, reason, None) if Nintendo is rate-limiting us, since
    that says nothing about detection; otherwise (False, None,
    summary_of_attempts, None). A pass proves the positive path: the checker
    recognises a genuinely buyable item. Checking only that out-of-stock items
    read as out of stock proves nothing, and is exactly how the 2026-09-10
    miss went unnoticed.
    """
    urls = (cfg.get("self_test") or {}).get("urls") or []
    tried = []
    for url in urls:
        try:
            buyable, detail = check_nintendo({"url": url})
        except RateLimited as e:
            return None, None, str(e), None
        except Exception as e:
            tried.append("{}: error {}".format(_label_from_url(url), e))
            continue
        if buyable:
            return True, _label_from_url(url), detail, url
        tried.append("{}: {}".format(_label_from_url(url), detail))
    return False, None, "; ".join(tried) or "no self_test urls configured", None


# --------------------------------------------------------------------------
# Diagnostics and one-off commands
# --------------------------------------------------------------------------

def _describe(text):
    """Summarise a fetched page for diagnostics."""
    if not text:
        return "empty"
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    bits = [
        "{}b".format(len(text)),
        "title={!r}".format(re.sub(r"\s+", " ", title.group(1)).strip()[:70])
        if title else "no-title",
    ]
    if re.search(r"(px-captcha|Robot or human|blocked because we believe)", text, re.I):
        bits.append("PERIMETERX-CHALLENGE")
    bits.append("NEXT_DATA" if "__NEXT_DATA__" in text else "no-NEXT_DATA")
    return " ".join(bits)


def run_diagnostics(cfg):
    """
    Probe each fetch strategy against one link per HTML-fetched store and
    report what came back. Run with --diagnose; intended for debugging blocks
    from CI, where the runner's IP behaves differently from a home connection.

    Paused and skipped links are probed too, so a store switched off because
    it was blocked can be re-tested without editing config.
    """
    lines = []

    def out(msg):
        log(msg)
        lines.append(msg)

    def finish():
        # Written to a file (and committed by the workflow) because Actions
        # logs need authentication to read, and this output is the whole point.
        with open(DIAG_PATH, "w", encoding="utf-8") as f:
            f.write("Diagnostics run {}\n\n".format(now_iso()))
            f.write("\n".join(lines) + "\n")
        log("diagnostics written to {}".format(os.path.basename(DIAG_PATH)))

    out("=== fetch diagnostics ===")

    stores = all_stores(cfg)
    probes, seen = [], set()
    for p in cfg.get("products") or []:
        for link in _links_of(p):
            sid = detect_store(link["url"], stores)
            if sid and sid not in seen and (sid in DIAGNOSABLE or not stores[sid]["builtin"]):
                seen.add(sid)
                probes.append((sid, link["url"]))
    if not probes:
        out("no diagnosable links configured")
        finish()
        return

    for sid, url in probes:
        out("")
        out("[{}] {}".format(sid, url))

        for label, browser in (("urllib-plain", False), ("urllib-browser", True)):
            try:
                polite_wait(url)
                req = urllib.request.Request(url)
                headers = BROWSER_HEADERS if browser else {"Accept": HTML_ACCEPT}
                for k, v in headers.items():
                    req.add_header(k, v)
                req.add_header("User-Agent", UA)
                with urllib.request.urlopen(req, timeout=25) as resp:
                    raw = resp.read()
                    status, final = resp.status, resp.geturl()
                text = raw.decode("utf-8", errors="replace")
                out("  {:16} HTTP {} -> {}".format(label, status, final))
                out("  {:16} {}".format("", _describe(text)))
            except Exception as e:
                out("  {:16} EXCEPTION {}".format(label, e))

        try:
            polite_wait(url)
            probe = subprocess.run(
                ["curl", "-sSL", "--compressed", "--max-time", "25", "-A", UA,
                 "-o", os.devnull,
                 "-w", "http_code=%{http_code} redirects=%{num_redirects} final=%{url_effective}",
                 url],
                capture_output=True, timeout=40)
            out("  {:16} {}".format("curl-trace", probe.stdout.decode("utf-8", "replace").strip()
                                    or probe.stderr.decode("utf-8", "replace").strip()[:200]))
        except Exception as e:
            out("  {:16} EXCEPTION {}".format("curl-trace", e))

        body = fetch_via_curl(url)
        out("  {:16} {}".format("curl-body", _describe(body)))

    finish()


def print_config_summary(cfg, warnings):
    """--check-config: what will be watched, and for whom. Makes no requests."""
    stores = all_stores(cfg)
    notif = cfg["notifications"]
    people = notif["people"]
    approved = cfg.get("approved_stores")
    print("config.json looks good (version 2).")
    print("People: {}   (status alerts go to: {})".format(
        ", ".join(people), ", ".join(notif.get("status_alerts_to") or people)))
    print("Approved stores: {}".format(", ".join(approved) if approved is not None else "all"))
    discord = notif.get("discord") or {}
    if discord.get("enabled"):
        print("Discord: on (needs the DISCORD_WEBHOOK_URL secret); topics that ping a role: {}".format(
            ", ".join(sorted(discord.get("topic_roles") or {})) or "none yet"))
    else:
        print("Discord: off")
    for p in cfg["products"]:
        paused = p.get("enabled", True) is False
        print("\n{}{}".format(p["name"], "  (paused)" if paused else ""))
        print("  notify: {}{}{}".format(
            ", ".join(p.get("notify") or people),
            "   topic: " + p["topic"] if p.get("topic") else "",
            "   (kept off Discord)" if p.get("discord") is False else ""))
        for link in _links_of(p):
            sid = detect_store(link["url"], stores)
            if paused or not link["enabled"]:
                tag = "paused"
            elif sid is None:
                tag = "SKIPPED - unknown store"
            elif approved is not None and sid not in approved:
                tag = "SKIPPED - not approved"
            elif not stores[sid].get("github_ok", True):
                tag = "home connection only"
            else:
                tag = "ok"
            print("  [{}] {}: {}".format(tag, stores[sid]["name"] if sid else _host(link["url"]),
                                         link["url"]))
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print("  - " + w)
    print("\nNo requests were made. Test a link against the live store with --check-link URL")


def check_link(cfg, url):
    """--check-link URL: show exactly what the watcher reads from one link."""
    stores = all_stores(cfg)
    sid = detect_store(url, stores)
    approved = cfg.get("approved_stores")
    print("Link:    {}".format(url))
    if sid is None:
        store = {"name": _host(url), "checker": check_generic, "builtin": False}
        print("Store:   {} is not a known store; trying the reader used for custom "
              "stores".format(_host(url)))
    else:
        store = stores[sid]
        print("Store:   {} ({}{})".format(store["name"], sid, "" if store["builtin"] else ", custom"))
        print("         approved: {}   checked from GitHub: {}".format(
            "yes" if approved is None or sid in approved else "NO - add it to approved_stores",
            "yes" if store.get("github_ok", True) else "no (home connection only)"))
    try:
        buyable, detail = store["checker"]({"url": url, "store": store})
    except Exception as e:
        print("Result:  COULD NOT READ - {}".format(e))
        if not store["builtin"]:
            print("         Tell it what to look for: add in_stock_text or sold_out_text "
                  "(words the page shows) to this store in custom_stores.")
        return 1
    print("Result:  {} - {}".format("BUYABLE" if buyable else "not buyable", detail))
    if sid is None:
        domain = re.sub(r"^www\.", "", _host(url))
        print('\nTo watch it, add {{"id": "...", "name": "...", "domains": ["{}"]}} to '
              "custom_stores and its id to approved_stores.".format(domain))
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def discovery_due(disc, state):
    """
    Discovery downloads two ~6 MB sitemaps, so it runs at most every
    `every_minutes` (default 60) instead of on every check. Known products
    are still stock-checked on every run.
    """
    if not disc.get("enabled"):
        return False
    every = disc.get("every_minutes", 60)
    last = state.get("last_discovery")
    if last:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last)).total_seconds()
            if age < every * 60:
                return False
        except (ValueError, TypeError):
            pass
    state["last_discovery"] = now_iso()
    return True


LOCAL_MIN_MINUTES = 5


def local_run_allowed():
    """
    Protect a home connection from accidental rapid re-runs.

    Never blocks on GitHub Actions. Locally, refuses to start if the script
    ran from this folder less than LOCAL_MIN_MINUTES ago, unless --force.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true" or "--force" in sys.argv:
        return True
    stamp = os.path.join(os.path.dirname(STATE_PATH), ".last_local_run")
    try:
        age = time.time() - os.path.getmtime(stamp)
        if age < LOCAL_MIN_MINUTES * 60:
            log("Not running: the last local run was {}s ago. Leave {} minutes "
                "between local runs so your home connection is not rate "
                "limited, or pass --force.".format(int(age), LOCAL_MIN_MINUTES))
            return False
    except OSError:
        pass
    with open(stamp, "w", encoding="utf-8") as f:
        f.write(now_iso())
    return True


def main():
    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"
    on_github = os.environ.get("GITHUB_ACTIONS") == "true"

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log("ERROR: config.json is missing or is not valid JSON")
        return 2
    errors, warnings = validate_config(cfg)
    if errors:
        log("config.json needs fixing:")
        for e in errors:
            log("  - " + e)
        return 2

    # One-off commands. --check-config makes no requests at all.
    if "--check-config" in sys.argv:
        print_config_summary(cfg, warnings)
        return 0
    for w in warnings:
        log("warning: " + w)
    apply_politeness(cfg)
    if "--check-link" in sys.argv:
        i = sys.argv.index("--check-link")
        if i + 1 >= len(sys.argv):
            log("usage: python3 zelda_watch.py --check-link URL")
            return 2
        return check_link(cfg, sys.argv[i + 1])

    if not local_run_allowed():
        return 3

    # Diagnostics need no topic and touch no state.
    if "--diagnose" in sys.argv:
        run_diagnostics(cfg)
        return 0

    notif = cfg["notifications"]
    people = notif["people"]
    status_to = notif.get("status_alerts_to") or people
    server = notif.get("server", "https://ntfy.sh")
    try:
        topics = resolve_topics(cfg)
    except ValueError as e:
        log("ERROR: {}".format(e))
        return 2
    discord_cfg = notif.get("discord") or {}
    webhook = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    use_discord = bool(discord_cfg.get("enabled"))
    discord_problem = None
    if use_discord and not webhook.startswith(DISCORD_WEBHOOK_PREFIXES):
        discord_problem = ("Discord is on in config.json, but the DISCORD_WEBHOOK_URL secret is "
                           + ("missing" if not webhook else "not a Discord webhook link"))
        log("warning: {}; Discord alerts are skipped".format(discord_problem))
        use_discord = False
    roles = discord_cfg.get("topic_roles") or {}
    status_to_discord = bool(discord_cfg.get("status_alerts"))

    if not topics and not use_discord and not dry_run:
        log("ERROR: nowhere to send alerts. Add the NTFY_TOPIC repository secret (one "
            "person), NTFY_TOPICS (several people), or set up Discord (see README.md).")
        return 2
    missing = [p for p in people if p not in topics]
    if missing and not dry_run:
        log("warning: no ntfy topic for {} - add them to the NTFY_TOPICS secret; their "
            "alerts are not being delivered".format(", ".join(missing)))

    def send(batch):
        if dry_run:
            log("DRY_RUN: would send {} notification(s)".format(len(batch)))
        for a in batch:
            # Product alerts say whether they belong on Discord; status alerts
            # (heartbeat, problems) follow discord.status_alerts.
            to_discord = use_discord and a.get("discord", status_to_discord)
            role = roles.get(a.get("topic") or "")
            if dry_run:
                log("  [{}] {} -> {}{}".format(
                    a["priority"], a["title"], ", ".join(a["to"]),
                    " + Discord" + (" (ping {})".format(a["topic"]) if role else "")
                    if to_discord else ""))
                continue
            # One push per topic, even if two people share a topic.
            for topic in sorted({topics[p] for p in a["to"] if p in topics}):
                notify(topic, server, a["title"], a["body"], a["priority"], a["tags"], a["click"])
            if to_discord and post_discord(webhook, discord_payload(a, role)):
                log("  -> Discord: {}".format(a["title"]))

    # Self-test: known-good case through the real checker and real alert path.
    # Touches no state.
    if "--self-test" in sys.argv:
        passed, label, detail, url = self_test(cfg)
        if passed is None:
            send([{"to": status_to, "discord": True, "title": "SELF-TEST SKIPPED",
                   "body": "Nintendo is rate-limiting this connection, so the test "
                           "could not run. Try again later.\n\n" + detail,
                   "priority": "default", "tags": "hourglass", "click": actions_url()}])
            log("self-test SKIPPED: {}".format(detail))
            return 1
        if passed:
            alert = stock_alert(label, [(STORES["nintendo"]["name"], detail, url)])
            alert.update(
                to=status_to, discord=True, title="SELF-TEST " + alert["title"],
                # On Discord, ping self_test.topic's role so the test proves
                # the ping as well as the post.
                topic=(cfg.get("self_test") or {}).get("topic"),
                body="This is a test. The watcher checked an item that is in stock "
                     "right now and detected it correctly, so a real restock will "
                     "arrive exactly like this.\n\n" + alert["body"])
            send([alert])
            log("self-test PASSED: {} | {}".format(label, detail))
            return 0
        send([{"to": status_to, "discord": True, "title": "SELF-TEST FAILED",
               "body": "None of the items that are normally in stock were detected "
                       "as buyable, so real restocks may be missed.\n\n" + detail,
               "priority": "high", "tags": "warning", "click": actions_url()}])
        log("self-test FAILED: {}".format(detail))
        return 1

    state = load_json(STATE_PATH, {})
    migrate_state(cfg, state)
    _cooldowns.update({h: t for h, t in (state.get("cooldowns") or {}).items()
                       if t > time.time()})
    links_state = state.setdefault("links", {})
    stores = all_stores(cfg)
    disc = cfg.get("discovery") or {}
    alerts = []
    events = {}   # product name -> what happened to it this run

    def event(product, to, topic=None, discord=True):
        return events.setdefault(product, {"to": to, "topic": topic, "discord": discord,
                                           "in_stock": [], "listed": []})

    topic_of = {p["name"]: (p.get("topic"), p.get("discord", True) is not False)
                for p in cfg.get("products") or []}

    # ---- 1. Nintendo new-listing discovery ------------------------------
    # Runs first, so a product discovered this run is also stock-checked
    # this run rather than on the next one.
    try:
        if discovery_due(disc, state):
            new_urls = discover_nintendo_ca(cfg, state)
            state.pop("discovery_last_error", None)
        else:
            new_urls = []
    except Exception as e:
        new_urls = []
        log("discovery: ERROR ({})".format(e))
        state["discovery_last_error"] = str(e)[:300]
    if disc.get("auto_watch"):
        auto = state.setdefault("auto_watch", [])
        auto.extend(u for u in new_urls if u not in auto)

    # ---- 2. Check every watched link --------------------------------------
    watch, skipped, configured = build_watchlist(cfg, state, stores, on_github)
    by_key = {w["key"]: w for w in watch}
    for u in new_urls:
        w = by_key.get(link_key(u))
        if w:
            event(w["product"], w["notify"], w.get("topic"), w.get("discord", True))["listed"].append(w)
        else:
            name = configured.get(link_key(u)) or _label_from_url(u)
            topic, on_discord = topic_of.get(name, (disc.get("topic"), True))
            event(name, disc.get("notify") or people, topic, on_discord)["listed"].append(
                {"key": link_key(u), "url": u, "unwatched": True})

    for w in watch:
        st = links_state.setdefault(w["key"], {})
        label = "{} ({})".format(w["product"], w["store"]["name"])
        try:
            buyable, detail = w["store"]["checker"](w)
        except RateLimited as e:
            # Not a breakage: the store asked us to slow down and we are
            # complying, so it does not count toward the "not working" alert.
            st["last_error"] = str(e)[:300]
            log("{}: PAUSED ({})".format(label, e))
            continue
        except Exception as e:
            fails = st.get("fails", 0) + 1
            st["fails"] = fails
            st["last_error"] = str(e)[:300]
            log("{}: ERROR ({}) [{} consecutive]".format(label, e, fails))

            # Warn once when a link goes dark, so it cannot fail silently.
            if fails >= BROKEN_AFTER and not st.get("warned_broken"):
                st["warned_broken"] = True
                alerts.append({
                    "to": status_to, "title": "Watcher problem: " + label,
                    "body": "This link has failed {} checks in a row.\n\nLast error: {}"
                            "\n\nIt may be blocked or the page layout changed. Every other "
                            "link is still being checked.".format(fails, e),
                    "priority": "low", "tags": "warning", "click": w["url"]})
            continue

        was = st.get("buyable")
        st["fails"] = 0
        st["warned_broken"] = False
        st["last_error"] = None
        st["buyable"] = buyable
        st["detail"] = detail
        log("{}: {} | {}".format(label, "BUYABLE" if buyable else "no", detail))

        # Alert only on the transition into buyable, so you get one push per
        # drop rather than one every five minutes.
        if buyable and was is not True:
            st["last_alerted"] = now_iso()
            event(w["product"], w["notify"], w.get("topic"),
                  w.get("discord", True))["in_stock"].append((w, detail))
        elif was is True and not buyable:
            log("  (went out of stock again)")

    # One push per product, however many of its links changed this run.
    listing_alerts = []
    for product, e in events.items():
        if e["in_stock"]:
            alert = stock_alert(product, [(w["store"]["name"], d, w["url"])
                                          for w, d in e["in_stock"]],
                                new_listing=bool(e["listed"]))
            alert.update(to=e["to"], topic=e["topic"], discord=e["discord"])
            alerts.append(alert)
            continue
        for w in e["listed"]:
            st = links_state.get(w["key"], {})
            if w.get("unwatched"):
                status = None
            elif st.get("last_error"):
                status = "could not check it yet ({})".format(st["last_error"][:80])
            elif st.get("buyable"):
                status = "buyable ({})".format(st.get("detail"))
            else:
                status = "sold out ({})".format(st.get("detail", "?"))
            alert = listing_alert(product, w["url"], status)
            alert.update(to=e["to"], topic=e["topic"], discord=e["discord"])
            listing_alerts.append(alert)
    alerts += listing_alerts[:10]
    if len(listing_alerts) > 10:
        alerts.append({"to": disc.get("notify") or people, "title": "NEW LISTINGS",
                       "topic": disc.get("topic"), "discord": True,
                       "body": "{} more new matching products appeared on the Nintendo "
                               "store.".format(len(listing_alerts) - 10),
                       "priority": "high", "tags": "sparkles",
                       "click": "https://www.nintendo.com/{}/store/".format(
                           disc.get("region", "en-ca"))})

    # ---- 3. Heartbeat ----------------------------------------------------
    # Without this, "no notifications" is ambiguous: it could mean nothing is
    # in stock, or it could mean the watcher died three weeks ago. A periodic
    # all-clear makes silence trustworthy.
    hb_hours = cfg.get("heartbeat_hours", 24)
    if hb_hours:
        last_hb = state.get("last_heartbeat")
        due = True
        if last_hb:
            try:
                elapsed = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last_hb)).total_seconds()
                due = elapsed >= hb_hours * 3600
            except (ValueError, TypeError):
                due = True
        if due:
            state["last_heartbeat"] = now_iso()
            lines = ["Still watching {} product(s).".format(len({w["product"] for w in watch}))]
            current = None
            for w in watch:
                if w["product"] != current:
                    current = w["product"]
                    lines.append("\n" + current)
                st = links_state.get(w["key"], {})
                if st.get("last_error"):
                    lines.append("  x {}: NOT WORKING - {}".format(
                        w["store"]["name"], st["last_error"][:60]))
                else:
                    lines.append("  - {}: {}".format(w["store"]["name"], st.get("detail", "?")))
            if skipped:
                counts = {}
                for s in skipped:
                    counts[(s["store"], s["reason"])] = counts.get((s["store"], s["reason"]), 0) + 1
                lines.append("\nNot checked: " + "; ".join(
                    "{} x{} ({})".format(store, n, reason)
                    for (store, reason), n in sorted(counts.items())))
            if missing:
                lines.append("\nNo ntfy topic for: {}".format(", ".join(missing)))
            if discord_problem:
                lines.append("\n" + discord_problem + ".")
            body = "\n".join(lines)

            # Re-prove the positive path every heartbeat: an all-clear only
            # means something if the watcher can still recognise an in-stock item.
            if (cfg.get("self_test") or {}).get("urls"):
                passed, st_label, st_detail, _ = self_test(cfg)
                if passed:
                    body += ("\n\nSelf-test OK: detection confirmed on an "
                             "in-stock item ({}).".format(st_label))
                elif passed is None:
                    body += "\n\nSelf-test skipped: {}".format(st_detail)
                else:
                    body += "\n\nSelf-test FAILED - see the separate alert."
                    alerts.append({
                        "to": status_to, "title": "Self-test failed",
                        "body": "The heartbeat re-checked items that are normally in "
                                "stock and none read as buyable. Either they all sold "
                                "out at once, or detection is broken and real restocks "
                                "would be missed.\n\n" + st_detail,
                        "priority": "high", "tags": "warning", "click": actions_url()})

            # Report the actual check rate against what the schedule promises,
            # so a silently throttled or stalled cron is visible.
            ran = count_recent_runs(last_hb)
            if ran is not None:
                expected = int(hb_hours * 60 / 5)
                body += "\n\n{} checks in the last {}h (expected ~{}).".format(
                    ran, hb_hours, expected)
                if ran < expected * 0.5:
                    body += (" GitHub is running this far less often than "
                             "scheduled; see 'Reliable scheduling' in the README.")
            alerts.append({"to": status_to, "title": "Watcher still alive", "body": body,
                           "priority": "min", "tags": "hourglass_flowing_sand",
                           "click": actions_url()})

    # ---- 4. Send ---------------------------------------------------------
    send(alerts)

    # Forget links that are no longer configured or auto-watched.
    keep = set(configured) | {link_key(u) for u in state.get("auto_watch", [])}
    for k in [k for k in links_state if k not in keep]:
        del links_state[k]

    # Persist cooldowns so the next run keeps respecting them.
    live = {h: int(t) for h, t in _cooldowns.items() if t > time.time()}
    if live:
        state["cooldowns"] = live
    else:
        state.pop("cooldowns", None)

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")

    log("done: {} alert(s), state saved".format(len(alerts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
