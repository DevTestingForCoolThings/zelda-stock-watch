# Zelda 40th Anniversary Stock Watcher (Canada)

Watches Canadian retailers for the **Nintendo Switch 2 – The Legend of Zelda 40th
Anniversary Edition** console, Pro Controller and accessories, and pushes a
notification to your phone the moment something becomes buyable.

Runs on GitHub Actions, so your computer does not need to be on. No API key, no
account beyond GitHub, no dependencies.

**Notify-only.** It will never add to cart or check out for you. You get a push
with a tappable link and you buy it yourself.

---

## What it watches

| Source | What it reads | Status from GitHub Actions |
|---|---|---|
| Nintendo CA | `schema.org/availability` in the product page | ✅ Working — server-rendered, no bot protection |
| Best Buy CA | `ecomm-api/availability` JSON endpoint | ✅ Working — clean public JSON |
| Amazon.ca | add-to-cart / `#outOfStock` markers | ✅ Working |
| EB Games CA | `schema.org/availability` in the product page | ❌ **Disabled** — 403 from datacenter IPs (Akamai) |
| Walmart.ca | `__NEXT_DATA__` product node | ❌ **Disabled** — blocked from datacenter IPs |

### Why Walmart and EB Games are disabled

Both are blocked from GitHub-s runner IP range. Walmart sits behind PerimeterX;
EB Games behind Akamai, which returns a flat 403. For Walmart, all
three fetch strategies (plain, browser headers, curl) get redirected to
`walmart.ca/blocked?...` with a "Verify Your Identity" challenge page.

Two different TLS stacks and three header profiles producing an identical result
means the block keys on **IP reputation**, not client fingerprint — so no
client-side change fixes it. Getting through would need a residential proxy,
which means a paid service and an API key.

The code and config entries are kept (just `"enabled": false`) so it can be
re-tested at any time with a diagnose run. Walmart works fine from a home
connection, so running this script locally still checks it.

Plus **new-listing discovery**: every run it scans the Nintendo Canada store
sitemap for product URLs matching Zelda/40th keywords. When Nintendo CA adds an
exclusive collectible, you get a push the first time its page exists — before
it has a URL anyone could have put on a watchlist.

### Note on Nintendo Canada and the console

Nintendo CA currently shows **"Find retailers"** for the console rather than a
cart button — they are not selling it directly. The watcher understands this and
will not report it as in stock just because the page exists. If Nintendo CA ever
opens direct sales, the button changes and you get alerted.

---

## Setup

### 1. Get the ntfy app and pick a topic

Install **ntfy** (free, no account): [iOS](https://apps.apple.com/app/ntfy/id1625396347) ·
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)

In the app, tap **+** and subscribe to a topic name. **Your topic name is your
password** — anyone who knows it can read your alerts, so make it long and
random, not `zelda`. For example:

```
zelda-ca-watch-x7k2m9qp4v
```

### 2. Create the GitHub repo

Make it **public** — see the cost warning below.

```bash
git init && git add . && git commit -m "Zelda stock watcher"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

### 3. Add your topic as a secret

In the repo: **Settings → Secrets and variables → Actions → New repository secret**

- Name: `NTFY_TOPIC`
- Value: your topic name (just the name, not the full URL)

Secrets are not visible to anyone browsing a public repo, and this script never
prints the topic to the logs.

### 4. Turn it on and test

Go to the **Actions** tab and enable workflows if prompted. Then run
**Zelda 40th stock watch → Run workflow** manually. Check the log — you should
see a status line per product. If something is in stock, your phone buzzes.

To test the notification path end to end without waiting for a drop, run this
with your own topic:

```bash
curl -H "Title: Test" -d "If you can see this, alerts work." ntfy.sh/YOUR-TOPIC-HERE
```

---

## Important: keep the repo public, or slow the schedule down

GitHub Actions is **free and unlimited on public repositories**. On a **private**
repo the Free plan gives you **2,000 minutes/month**, and every job is billed as
a minimum of one full minute.

At the default 5-minute cadence that is about **8,600 runs a month** — roughly
four times the private-repo allowance. So either:

- **Keep the repo public** (recommended — nothing sensitive is in it), or
- Make it private and change the cron in `.github/workflows/watch.yml` to
  `*/30 * * * *` (every 30 minutes, ~1,440 runs/month, fits the free tier).

Two other GitHub scheduling facts worth knowing:

- `*/5` is the **fastest** schedule GitHub allows, and under load GitHub can
  still delay a scheduled run by 5–15 minutes. For a hyped drop that sells out in
  90 seconds, this bot improves your odds but cannot guarantee a catch.
- GitHub **disables scheduled workflows after 60 days without repo activity**.
  The bot commits `state.json` whenever stock changes, which usually counts, but
  if things go quiet for two months, push any commit to re-arm it.

---

## Adding more products

Edit `config.json` and add an entry to `targets`.

**Best Buy** (easiest — find the SKU in the product URL):

```json
{
  "id": "bestbuy-ca-something",
  "enabled": true,
  "source": "bestbuy",
  "label": "Human readable name",
  "sku": "20149861",
  "url": "https://www.bestbuy.ca/en-ca/product/20149861"
}
```

To find a Best Buy SKU by name:

```bash
curl -s "https://www.bestbuy.ca/api/v2/json/search?query=zelda%2040th&lang=en-CA" | grep -o '"sku":"[0-9]*","name":"[^"]*"'
```

For `nintendo`, `amazon` and `walmart`, use `"source"` plus the product `"url"` —
no SKU needed.

Set `"enabled": false` to mute a target without deleting it.

### Widening discovery

`discovery.must_match_any` and `and_must_match_any` are substring filters against
the product URL. A URL must match **at least one from each list**. Loosen them to
catch more, tighten to reduce noise.

---

## Diagnosing a blocked source

If a source starts failing, run the workflow manually with the **diagnose**
checkbox ticked. It probes one target per HTML-fetched source using three fetch
strategies and reports HTTP status, redirect chain, final URL, page title and
block markers — then commits the result to `diagnostics.txt` so you can read it
without digging through Actions logs.

Disabled targets are probed too, so you can re-test Walmart without editing
config. Locally:

```bash
python3 zelda_watch.py --diagnose
```

Reading the output: a redirect to a `/blocked` URL means IP-reputation blocking
(not fixable client-side). A 403 means the same. A 200 with a small body and
`no-NEXT_DATA` means a JavaScript challenge. A 429 means you are being rate
limited — back off rather than retrying.

## Running it locally

Needs Python 3 (standard library only):

```bash
DRY_RUN=1 python3 zelda_watch.py
```

`DRY_RUN=1` checks everything and prints what it *would* send without notifying.
Drop it and set `NTFY_TOPIC` to actually push.

---

## How it avoids spamming you

- Alerts fire **only on the transition** into buyable, not every run while stock
  lasts. One push per drop.
- Walmart alerts include **price and seller**, so a marketplace reseller at triple
  MSRP is obvious at a glance.
- On the very first run, discovery records a silent baseline instead of alerting
  on all 42 products that already exist.
- If a source breaks or gets blocked, the other sources keep working, and you get
  one low-priority heads-up after 6 consecutive failures — so a scraper can never
  fail *silently* and leave you thinking everything is fine.

---

## Being a good citizen

`nintendo.com/robots.txt` allows general crawling (`User-agent: * / Allow: /`).
This checks a handful of URLs every few minutes with a normal browser
User-Agent — comparable to leaving a few tabs open. Please do not drop the
interval below 5 minutes or add dozens of targets; that is what gets IP ranges
blocked for everyone.
