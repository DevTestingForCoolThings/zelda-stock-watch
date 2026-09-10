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


def fetch(url, timeout=25, accept=HTML_ACCEPT, browser_like=False):
    """GET a URL, returning decoded text. Raises on failure."""
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
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
        try:
            out = subprocess.run(base + extra + [url],
                                 capture_output=True, timeout=timeout + 10)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode == 0 and out.stdout:
            return out.stdout.decode("utf-8", errors="replace")
        # Exit code 2 means curl rejected an option; retry without it.
        if out.returncode != 2:
            return None
    return None


# --------------------------------------------------------------------------
# Per-retailer availability checks
#
# Each returns (buyable: bool, detail: str).
# Each raises on a fetch/parse failure so the caller can mark the source broken
# rather than silently reporting "out of stock" forever.
# --------------------------------------------------------------------------

# schema.org values that mean you can hand over money right now
BUYABLE_SCHEMA = {"InStock", "PreOrder", "BackOrder", "LimitedAvailability", "OnlineOnly"}


def check_nintendo(target):
    """Nintendo CA product page. Server-rendered, exposes JSON-LD availability."""
    html = fetch(target["url"])

    m = re.search(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"', html)
    if not m:
        raise ValueError("no schema.org availability found (page layout changed?)")
    status = m.group(1)

    # Nintendo CA lists some hardware it does not sell directly, showing a
    # "Find retailers" button instead of a cart. Surface that distinction.
    has_cart = bool(re.search(r"Add to cart", html, re.I))
    only_retailers = bool(re.search(r"Find retailers", html, re.I)) and not has_cart

    buyable = status in BUYABLE_SCHEMA and not only_retailers
    detail = status
    if only_retailers:
        detail += " (Nintendo CA shows 'Find retailers', not sold direct)"
    elif has_cart:
        detail += " (Add to cart present)"
    return buyable, detail


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
    datacenter IPs (including GitHub Actions runners). We try three fetch
    strategies before giving up, since the block is inconsistent.
    """
    html = None
    attempts = [
        ("browser-headers", lambda: fetch(target["url"], browser_like=True)),
        ("plain", lambda: fetch(target["url"])),
        ("curl", lambda: fetch_via_curl(target["url"])),
    ]
    notes = []
    for name, attempt in attempts:
        try:
            candidate = attempt()
        except Exception as e:
            notes.append("{}: {}".format(name, e))
            continue
        if not candidate:
            notes.append("{}: empty response".format(name))
            continue
        if re.search(r"(px-captcha|Robot or human|blocked because we believe)",
                     candidate, re.I):
            notes.append("{}: PerimeterX challenge".format(name))
            continue
        if "__NEXT_DATA__" not in candidate:
            notes.append("{}: no __NEXT_DATA__ ({}b)".format(name, len(candidate)))
            continue
        html = candidate
        break

    # Report every strategy, not just the last: knowing which ones got a
    # challenge versus a redirect is what makes this debuggable from CI logs.
    if html is None:
        raise ValueError("Walmart unreachable [{}]".format("; ".join(notes)))

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
}


# --------------------------------------------------------------------------
# Nintendo CA new-product discovery
# --------------------------------------------------------------------------

def discover_nintendo_ca(cfg, state):
    """
    Scan the Nintendo CA store sitemap for product URLs matching our keywords.
    Anything never seen before is reported as a brand-new listing. This is how
    Nintendo-CA-exclusive collectibles get caught before they have a known URL.
    """
    d = cfg.get("discovery") or {}
    if not d.get("enabled"):
        return []

    xml = fetch(d["sitemap"], timeout=60, accept="application/xml,text/xml,*/*")
    urls = re.findall(r"<loc>\s*([^<\s]+/store/products/[^<\s]+)\s*</loc>", xml)
    if not urls:
        raise ValueError("sitemap returned no product URLs")

    must = [s.lower() for s in d.get("must_match_any", [])]
    also = [s.lower() for s in d.get("and_must_match_any", [])]
    skip = [s.lower() for s in d.get("ignore_containing", [])]

    matched = []
    for u in urls:
        lu = u.lower()
        if any(s in lu for s in skip):
            continue
        if must and not any(s in lu for s in must):
            continue
        if also and not any(s in lu for s in also):
            continue
        matched.append(u)

    matched = sorted(set(matched))
    seen = state.get("discovered") or []

    # First ever run: record a baseline silently rather than alerting on
    # every product that already exists.
    if not seen:
        state["discovered"] = matched
        log("discovery: baseline recorded ({} matching products)".format(len(matched)))
        return []

    new = [u for u in matched if u not in seen]
    state["discovered"] = sorted(set(seen) | set(matched))
    log("discovery: {} matching, {} new".format(len(matched), len(new)))
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
    Probe each fetch strategy against the Walmart targets and print what came
    back. Run with --diagnose; intended for debugging blocks from CI, where
    the runner's IP behaves differently from a home connection.
    """
    log("=== fetch diagnostics ===")
    targets = [t for t in cfg.get("targets", []) if t.get("source") == "walmart"]
    if not targets:
        log("no walmart targets configured")
        return

    for t in targets[:1]:
        url = t["url"]
        log("target: {}".format(url))

        for label, browser in (("urllib-plain", False), ("urllib-browser", True)):
            try:
                req = urllib.request.Request(url)
                headers = BROWSER_HEADERS if browser else {"Accept": HTML_ACCEPT}
                for k, v in headers.items():
                    req.add_header(k, v)
                req.add_header("User-Agent", UA)
                with urllib.request.urlopen(req, timeout=25) as resp:
                    raw = resp.read()
                    status, final = resp.status, resp.geturl()
                text = raw.decode("utf-8", errors="replace")
                log("  {:16} HTTP {} -> {}".format(label, status, final))
                log("  {:16} {}".format("", _describe(text)))
            except Exception as e:
                log("  {:16} EXCEPTION {}".format(label, e))

        try:
            probe = subprocess.run(
                ["curl", "-sSL", "--compressed", "--max-time", "25", "-A", UA,
                 "-o", os.devnull,
                 "-w", "http_code=%{http_code} redirects=%{num_redirects} final=%{url_effective}",
                 url],
                capture_output=True, timeout=40)
            log("  {:16} {}".format("curl-trace", probe.stdout.decode("utf-8", "replace").strip()
                                    or probe.stderr.decode("utf-8", "replace").strip()[:200]))
        except Exception as e:
            log("  {:16} EXCEPTION {}".format("curl-trace", e))

        body = fetch_via_curl(url)
        log("  {:16} {}".format("curl-body", _describe(body)))


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log("ERROR: config.json missing or invalid")
        return 2

    # Diagnostics need no topic and touch no state.
    if "--diagnose" in sys.argv:
        run_diagnostics(cfg)
        return 0

    if not topic and not dry_run:
        log("ERROR: NTFY_TOPIC is not set. Add it as a GitHub Actions secret.")
        return 2
    state = load_json(STATE_PATH, {})
    server = cfg.get("ntfy_server", "https://ntfy.sh")

    statuses = state.setdefault("targets", {})
    alerts = []   # (title, body, priority, tags, click)

    # ---- 1. Known product watchlist -------------------------------------
    for t in cfg.get("targets", []):
        if not t.get("enabled", True):
            continue
        tid = t["id"]
        label = t.get("label", tid)
        st = statuses.setdefault(tid, {})
        checker = CHECKERS.get(t.get("source"))
        if checker is None:
            log("{}: unknown source '{}', skipping".format(tid, t.get("source")))
            continue

        try:
            buyable, detail = checker(t)
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
            alerts.append((
                "IN STOCK: " + label,
                "{}\n\nStatus: {}\n\nGo buy it now:\n{}".format(
                    label, detail, t.get("url", "")),
                "max", "rotating_light,shopping_cart",
                t.get("url", ""),
            ))
        elif was is True and not buyable:
            log("  (went out of stock again)")

    # ---- 2. Nintendo CA new-listing discovery ---------------------------
    try:
        new_urls = discover_nintendo_ca(cfg, state)
        for u in new_urls[:10]:
            name = u.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
            alerts.append((
                "NEW on Nintendo CA",
                "A new matching product page just appeared on the Nintendo "
                "Canada store:\n\n{}\n\n{}".format(name, u),
                "high", "sparkles,new", u,
            ))
        if len(new_urls) > 10:
            alerts.append((
                "NEW on Nintendo CA",
                "{} more new matching products appeared. Check the store.".format(
                    len(new_urls) - 10),
                "high", "sparkles", "https://www.nintendo.com/en-ca/store/",
            ))
    except Exception as e:
        log("discovery: ERROR ({})".format(e))
        state["discovery_last_error"] = str(e)[:300]

    # ---- 3. Send ---------------------------------------------------------

    if dry_run:
        log("DRY_RUN: would send {} notification(s)".format(len(alerts)))
        for a in alerts:
            log("  [{}] {}".format(a[2], a[0]))
    else:
        for title, body, prio, tags, click in alerts:
            notify(topic, server, title, body, prio, tags, click)

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")

    log("done: {} alert(s), state saved".format(len(alerts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
