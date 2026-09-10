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

def fetch(url, timeout=25,
          accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"):
    """GET a URL, returning decoded text. Raises on failure."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", accept)
    req.add_header("Accept-Language", "en-CA,en;q=0.9")
    req.add_header("Accept-Encoding", "gzip, identity")
    req.add_header("Cache-Control", "no-cache")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw.decode("utf-8", errors="replace")


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

    Best-effort overall: the page sits behind PerimeterX, which may block
    datacenter IPs (including GitHub Actions runners).
    """
    html = fetch(target["url"])

    if re.search(r"(px-captcha|Robot or human|blocked because we believe)", html, re.I):
        raise ValueError("blocked by Walmart bot check (PerimeterX)")

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


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"

    if not topic and not dry_run:
        log("ERROR: NTFY_TOPIC is not set. Add it as a GitHub Actions secret.")
        return 2

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log("ERROR: config.json missing or invalid")
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
