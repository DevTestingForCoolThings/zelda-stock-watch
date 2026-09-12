#!/usr/bin/env python3
"""
Zelda 40th Anniversary stock watcher (Canada).

Notify-only. Checks Nintendo CA + Best Buy CA + Amazon.ca + Walmart.ca for the
Switch 2 Zelda 40th Anniversary console, Pro Controller and accessories, and
pushes to your phone via ntfy.sh when something becomes buyable.

Also watches the Nintendo CA store sitemap so that Nintendo-CA-exclusive
collectibles are caught the moment their product page first appears.

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
    # a paused target does not rewrite state.json - and commit - every run.
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
# Per-retailer availability checks
#
# Each returns (buyable: bool, detail: str).
# Each raises on a fetch/parse failure so the caller can mark the source broken
# rather than silently reporting "out of stock" forever.
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


def check_nintendo(target):
    """
    Nintendo store product page (any region; server-rendered).

    Primary signal: `isSalableQty` on the product's own __NEXT_DATA__ node,
    which is what drives the Add to cart button. Secondary: schema.org
    availability in the JSON-LD. The two agreed on all 17 items sampled in
    September 2026, in stock and out.

    Deliberately NOT used: the presence of "Find retailers" or "Add to cart"
    text. Add to cart is rendered client-side and never appears in the server
    HTML, and Find retailers is a secondary link shown even on items Nintendo
    sells directly. An earlier version treated "Find retailers without Add to
    cart" as "not sold direct", which suppressed a real InStock reading on
    the Zelda console on 2026-09-10 and missed the restock.
    """
    html = fetch(target["url"])

    m = re.search(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"', html)
    schema = m.group(1) if m else None
    product = _nintendo_product(html, _nintendo_sku(target["url"]))

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


def check_ebgames(target):
    """
    EB Games Canada. Server-rendered and exposes the same schema.org
    availability that Nintendo CA does.
    """
    html, _ = fetch_with_fallbacks(target["url"], "schema.org")

    m = re.search(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"', html)
    if not m:
        raise ValueError("no schema.org availability found (blocked or layout changed?)")
    status = m.group(1)

    p = re.search(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]{2})?)', html)
    price = "${}".format(p.group(1)) if p else "?"

    buyable = status in BUYABLE_SCHEMA
    return buyable, "{} | {}".format(status, price)


def check_bestbuy(target):
    """Best Buy CA has a clean public availability JSON endpoint."""
    sku = target["sku"]
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


def check_amazon(target):
    """
    Amazon.ca product page.

    The reliable signal is the presence of the add-to-cart control together
    with the absence of the #outOfStock box. The visible availability text is
    only used for the human-readable detail line, because parsing it alone
    picks up div attributes rather than the message.
    """
    html = fetch(target["url"])

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
    return buyable, detail[:160]


def _walmart_item_id(url):
    """Walmart.ca product URLs end in the usItemId, e.g. /ip/Some-Name/4LPRUXHD1MMQ"""
    return url.rstrip("/").rsplit("/", 1)[-1]


def check_walmart(target):
    """
    Walmart.ca.

    Reads the main product node out of __NEXT_DATA__ and verifies its
    usItemId matches the URL, so a recommended or related product further
    down the page cannot trigger a false in-stock alert.

    Best-effort overall: the page sits behind PerimeterX, which blocks
    datacenter IPs (including GitHub Actions runners).
    """
    html, _ = fetch_with_fallbacks(target["url"], "__NEXT_DATA__")

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("no __NEXT_DATA__ in page (blocked or layout changed?)")
    try:
        data = json.loads(m.group(1))
        product = data["props"]["pageProps"]["initialData"]["data"]["product"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("could not locate product node: {}".format(e))

    want = _walmart_item_id(target["url"])
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


CHECKERS = {
    "nintendo": check_nintendo,
    "bestbuy": check_bestbuy,
    "amazon": check_amazon,
    "walmart": check_walmart,
    "ebgames": check_ebgames,
}

# Sources fetched as HTML, and therefore worth probing with --diagnose when a
# block is suspected. Best Buy is excluded: it uses a JSON API, and its HTML
# product pages 403 even from a residential IP.
DIAGNOSABLE = ("ebgames", "walmart", "amazon", "nintendo")


# --------------------------------------------------------------------------
# Nintendo CA new-product discovery
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


# --------------------------------------------------------------------------
# Main
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
        log("warning: could not read {} ({}), starting fresh".format(path, e))
        return default


def count_recent_runs(since_iso):
    """
    Count workflow runs since a timestamp, via the public Actions API.

    Turns the heartbeat into a real health report: it says whether the
    schedule is actually firing at the expected rate, which is the one thing
    the bot cannot otherwise observe about itself. Best-effort - returns None
    if the repo is unknown or the API is unreachable.
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
    Probe each fetch strategy against one target per HTML-fetched source and
    report what came back. Run with --diagnose; intended for debugging blocks
    from CI, where the runner's IP behaves differently from a home connection.

    Disabled targets are probed too, so a source switched off because it was
    blocked can be re-tested without editing config.
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

    # One representative target per source, disabled ones included.
    probes = []
    for source in DIAGNOSABLE:
        for t in cfg.get("targets", []):
            if t.get("source") == source:
                probes.append(t)
                break
    if not probes:
        out("no diagnosable targets configured")
        finish()
        return

    for t in probes:
        url = t["url"]
        out("")
        out("[{}] {}".format(t.get("source"), url))

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


def actions_url():
    """Link for pushes about the bot itself: this repo's Actions tab."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return "https://github.com/{}/actions".format(repo) if repo else ""


def stock_alert(label, detail, url):
    """
    Build the in-stock push. The one place it is built, shared by real alerts
    and the self-test, so a passing self-test exercises the exact path a real
    restock takes.
    """
    return (
        "IN STOCK: " + label,
        "{}\n\nStatus: {}\n\nGo buy it now:\n{}".format(label, detail, url),
        "max", "rotating_light,shopping_cart", url,
    )


def _label_from_url(url):
    slug = re.sub(r"-\d{5,}$", "", _product_slug(url) or url)
    return slug.replace("-", " ").strip().title()


def self_test(cfg):
    """
    Run the real Nintendo checker against items configured as normally in stock.

    Returns (passed, label, detail, url) for the first item detected as
    buyable; (None, None, reason, None) if Nintendo is rate-limiting us, since
    that says nothing about detection; otherwise (False, None,
    summary_of_attempts, None). A pass proves the
    positive path: the checker recognises a genuinely buyable item. Checking
    only that out-of-stock items read as out of stock proves nothing, and is
    exactly how the 2026-09-10 miss went unnoticed.
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
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log("ERROR: config.json missing or invalid")
        return 2

    apply_politeness(cfg)
    if not local_run_allowed():
        return 3

    # Diagnostics need no topic and touch no state.
    if "--diagnose" in sys.argv:
        run_diagnostics(cfg)
        return 0

    if not topic and not dry_run:
        log("ERROR: NTFY_TOPIC is not set. Add it as a GitHub Actions secret.")
        return 2
    server = cfg.get("ntfy_server", "https://ntfy.sh")

    def send(batch):
        if dry_run:
            log("DRY_RUN: would send {} notification(s)".format(len(batch)))
            for a in batch:
                log("  [{}] {}".format(a[2], a[0]))
            return
        for title, body, prio, tags, click in batch:
            notify(topic, server, title, body, prio, tags, click)

    # Self-test: known-good case through the real checker and real alert path.
    # Touches no state.
    if "--self-test" in sys.argv:
        passed, label, detail, url = self_test(cfg)
        if passed is None:
            send([(
                "SELF-TEST SKIPPED",
                "Nintendo is rate-limiting this connection, so the test could "
                "not run. Try again later.\n\n" + detail,
                "default", "hourglass", actions_url(),
            )])
            log("self-test SKIPPED: {}".format(detail))
            return 1
        if passed:
            title, body, prio, tags, click = stock_alert(label, detail, url)
            send([(
                "SELF-TEST " + title,
                "This is a test. The watcher checked an item that is in stock "
                "right now and detected it correctly, so a real restock will "
                "arrive exactly like this.\n\n" + body,
                prio, tags, click,
            )])
            log("self-test PASSED: {} | {}".format(label, detail))
            return 0
        send([(
            "SELF-TEST FAILED",
            "None of the items that are normally in stock were detected as "
            "buyable, so real restocks may be missed.\n\n" + detail,
            "high", "warning", actions_url(),
        )])
        log("self-test FAILED: {}".format(detail))
        return 1

    state = load_json(STATE_PATH, {})
    _cooldowns.update({h: t for h, t in (state.get("cooldowns") or {}).items()
                       if t > time.time()})
    statuses = state.setdefault("targets", {})
    alerts = []   # (title, body, priority, tags, click)
    disc = cfg.get("discovery") or {}

    # ---- 1. Nintendo new-listing discovery ------------------------------
    # Runs first, so a product discovered this run is also stock-checked
    # this run rather than five minutes (or on GitHub, hours) later.
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
    for u in new_urls[:10]:
        alerts.append((
            "NEW on Nintendo CA",
            "A new matching product page just appeared on the Nintendo "
            "store:\n\n{}\n\n{}{}".format(
                _label_from_url(u), u,
                "\n\nIt is now being stock-watched automatically."
                if disc.get("auto_watch") else ""),
            "high", "sparkles,new", u,
        ))
    if len(new_urls) > 10:
        alerts.append((
            "NEW on Nintendo CA",
            "{} more new matching products appeared. Check the store.".format(
                len(new_urls) - 10),
            "high", "sparkles", "https://www.nintendo.com/en-ca/store/",
        ))
    if disc.get("auto_watch"):
        auto = state.setdefault("auto_watch", [])
        auto.extend(u for u in new_urls if u not in auto)

    # ---- 2. Watchlist: configured targets plus auto-watched discoveries --
    watch = [t for t in cfg.get("targets", []) if t.get("enabled", True)]
    configured = {t.get("url", "").rstrip("/") for t in cfg.get("targets", [])}
    if disc.get("auto_watch"):
        for u in state.get("auto_watch", []):
            if u.rstrip("/") not in configured:
                watch.append({
                    "id": "auto:" + (_product_slug(u) or u),
                    "source": "nintendo",
                    "label": _label_from_url(u) + " (Nintendo, auto-watched)",
                    "url": u,
                })

    for t in watch:
        tid = t["id"]
        label = t.get("label", tid)
        st = statuses.setdefault(tid, {})
        checker = CHECKERS.get(t.get("source"))
        if checker is None:
            log("{}: unknown source '{}', skipping".format(tid, t.get("source")))
            continue

        try:
            buyable, detail = checker(t)
        except RateLimited as e:
            # Not a breakage: the store asked us to slow down and we are
            # complying, so it does not count toward the "source broken" alert.
            st["last_error"] = str(e)[:300]
            log("{}: PAUSED ({})".format(tid, e))
            continue
        except Exception as e:
            fails = st.get("fails", 0) + 1
            st["fails"] = fails
            st["last_error"] = str(e)[:300]
            log("{}: ERROR ({}) [{} consecutive]".format(tid, e, fails))

            # Warn once when a source goes dark, so it cannot fail silently.
            if fails >= BROKEN_AFTER and not st.get("warned_broken"):
                st["warned_broken"] = True
                alerts.append((
                    "Watcher problem: " + label,
                    "This source has failed {} checks in a row.\n\nLast error: {}"
                    "\n\nIt may be blocked or the page layout changed. Other "
                    "sources are still being checked.".format(fails, e),
                    "low", "warning", t.get("url", ""),
                ))
            continue

        was = st.get("buyable")
        st["fails"] = 0
        st["warned_broken"] = False
        st["last_error"] = None
        st["buyable"] = buyable
        st["detail"] = detail

        log("{}: {} | {}".format(tid, "BUYABLE" if buyable else "no", detail))

        # Alert only on the transition into buyable, so you get one push
        # per drop rather than one every five minutes.
        if buyable and was is not True:
            st["last_alerted"] = now_iso()
            alerts.append(stock_alert(label, detail, t.get("url", "")))
        elif was is True and not buyable:
            log("  (went out of stock again)")

    # ---- 3. Heartbeat ----------------------------------------------------
    # Without this, "no notifications" is ambiguous: it could mean nothing is
    # in stock, or it could mean the bot died three weeks ago. A periodic
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
            ok, broken = [], []
            for t in watch:
                st = statuses.get(t["id"], {})
                label = t.get("label", t["id"])
                if st.get("last_error"):
                    broken.append("x {} - {}".format(label, st["last_error"][:60]))
                else:
                    ok.append("- {}: {}".format(label, st.get("detail", "?")))
            body = "Still watching.\n\n" + "\n".join(ok)
            if broken:
                body += "\n\nNot working:\n" + "\n".join(broken)

            # Re-prove the positive path every heartbeat: an all-clear only
            # means something if the bot can still recognise an in-stock item.
            if (cfg.get("self_test") or {}).get("urls"):
                passed, st_label, st_detail, _ = self_test(cfg)
                if passed:
                    body += ("\n\nSelf-test OK: detection confirmed on an "
                             "in-stock item ({}).".format(st_label))
                elif passed is None:
                    body += "\n\nSelf-test skipped: {}".format(st_detail)
                else:
                    body += "\n\nSelf-test FAILED - see the separate alert."
                    alerts.append((
                        "Self-test failed",
                        "The heartbeat re-checked items that are normally in "
                        "stock and none read as buyable. Either they all sold "
                        "out at once, or detection is broken and real restocks "
                        "would be missed.\n\n" + st_detail,
                        "high", "warning", actions_url(),
                    ))

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
            alerts.append((
                "Zelda watcher still alive", body, "min", "hourglass_flowing_sand",
                actions_url(),
            ))

    # ---- 4. Send ---------------------------------------------------------
    send(alerts)

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
