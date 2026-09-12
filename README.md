# Zelda 40th Anniversary Stock Watcher (Canada)

Watches Canadian stores for the **Nintendo Switch 2 – The Legend of Zelda 40th
Anniversary Edition** console, Pro Controllers and accessories, and pushes a
notification to your phone the moment something becomes buyable. It also
spots new Zelda products the first time they appear on the Nintendo Canada
store, and starts watching them automatically.

Runs on GitHub Actions, so your computer does not need to be on. No
dependencies (Python standard library only) and no accounts beyond GitHub:
notifications go through [ntfy](https://ntfy.sh), which needs no sign-up.

**Notify-only.** It will never add to cart or check out for you. You get a push
with a tappable link and you buy it yourself.

---

## What it watches

| Product | Nintendo CA | Best Buy CA | Amazon.ca |
|---|:---:|:---:|:---:|
| Switch 2 – Zelda 40th Anniversary console | ✅ | ✅ | ✅ |
| Zelda 40th Pro Controller | ✅ | ✅ | |
| Zelda 40th Pro Controller + display stand (Nintendo exclusive) | ✅ | | |
| Zelda 40th carrying case + screen protector | ✅ | ✅ | |
| Any new Zelda product that appears on Nintendo CA | ✅ auto | | |

### How each store is read

| Source | Signal | Status from GitHub Actions |
|---|---|---|
| Nintendo (any region) | `isSalableQty` on the product's own `__NEXT_DATA__` node, cross-checked with `schema.org/availability` | ✅ Working |
| Best Buy CA | `ecomm-api/availability` JSON endpoint | ✅ Working |
| Amazon.ca | add-to-cart / `#outOfStock` markers | ✅ Working |
| EB Games CA | `schema.org/availability` | ❌ Disabled: 403 from datacenter IPs (Akamai) |
| Walmart.ca | `__NEXT_DATA__` product node | ❌ Disabled: PerimeterX blocks datacenter IPs |

**Nintendo**: `isSalableQty` is the flag that drives the store's Add to cart
button. It was checked against schema.org on 17 products in September 2026, in
stock and out, and agreed every time. If the two ever disagree, the watcher
alerts anyway and says so in the notification, because for a restock bot a
missed drop costs far more than one spurious push.

Pre-orders count as buyable. Unreleased items show a **Pre-purchase** button
instead of Add to cart; they alert like anything else, with
`pre-order open, ships <date>` in the notification so you know what you're buying.

Two things on Nintendo pages look like stock signals but are not, and the
watcher deliberately ignores them:

- **"Add to cart"** is rendered in the browser, so it never appears in the page
  the server sends, even for items that are in stock.
- **"Find retailers"** is a secondary link shown *next to* Add to cart on items
  Nintendo sells directly. It does not mean "not sold here".

An earlier version relied on those two strings and missed a real console restock
as a result. See [What went wrong on 10 September 2026](#what-went-wrong-on-10-september-2026).

**Walmart and EB Games** block GitHub's runner IP range. For Walmart, three
fetch strategies (plain, browser headers, curl) all get redirected to
`walmart.ca/blocked?...`. Two TLS stacks and three header profiles producing the
same result means the block keys on **IP reputation**, which no client-side
change fixes. Their config entries are kept with `"enabled": false`, so a
diagnose run can re-test them, and both still work when the script runs from a
home connection.

### New-listing discovery

Every run scans the **Canadian and US** Nintendo store sitemaps for product URLs
matching the Zelda keywords in `config.json`. Anything matching that is listed
only in the US sitemap is checked on the Canadian store directly, and it counts
as discovered once the Canadian page exists.

The second sitemap is there because the Canadian one lags. In September 2026 the
Zelda Pro Controllers and carrying case were live on the Canadian store for days
while missing from its sitemap, even though the US sitemap listed them.

With `"auto_watch": true`, every newly discovered product is then stock-checked
on every run. That means you are told when it **appears**, and again when it
becomes **buyable**.

---

## Setup

### 1. Get the ntfy app and pick a topic

Install **ntfy** (free, no account): [iOS](https://apps.apple.com/app/ntfy/id1625396347) ·
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)

In the app, tap **+** and subscribe to a topic name. **Your topic name is your
password**: anyone who knows it can read your alerts, so make it long and
random, not `zelda`. For example:

```
zelda-ca-watch-x7k2m9qp4v
```

### 2. Fork or create the repo

Make it **public**. See [the cost note](#keep-the-repo-public).

### 3. Add your topic as a secret

In the repo: **Settings → Secrets and variables → Actions → New repository secret**

- Name: `NTFY_TOPIC`
- Value: your topic name only (not the `https://ntfy.sh/...` URL)

Use the **Secrets** tab, not **Variables**: variables are visible to anyone
reading a public repo. The script never prints the topic to the logs.

### 4. Run the self-test

Go to **Actions → Zelda 40th stock watch → Run workflow**, tick **Self-test**,
and run it. Within a minute your phone should show:

> **SELF-TEST IN STOCK: Nintendo Switch 2 Pro Controller**

This checks a few Nintendo items that are normally in stock (`self_test.urls` in
`config.json`), using the real checker and the real alert path. If it arrives,
detection **and** delivery both work. If none of those items read as in stock,
you get **SELF-TEST FAILED** and the job fails, so a broken checker cannot pass
unnoticed.

The heartbeat repeats the self-test every time it runs, so the all-clear you
receive every few hours also confirms detection still works.

### 5. Make the schedule reliable

Do this step. See [Reliable scheduling](#reliable-scheduling).

---

## Reliable scheduling

The workflow asks GitHub to run every 5 minutes. **In practice GitHub ran it
every 2 to 4 hours**: 14 scheduled runs in 46 hours on this repo in September
2026. GitHub documents that scheduled workflows can be delayed or dropped under
load, and a restock that sells out in an hour can fall entirely between two
runs.

The fix is to trigger the workflow from outside on a real timer. GitHub's API
can start a workflow run on request; a free cron service can make that request
every 5 minutes.

**1. Create a narrowly scoped GitHub token**

GitHub → your avatar → **Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**

- **Resource owner**: the account or organisation that owns the repo
- **Repository access**: *Only select repositories* → this repo
- **Permissions → Repository → Actions**: *Read and write*
- **Expiration**: pick a date and put a reminder in your calendar

That token can start this one workflow and nothing else. If the owner is an
organisation, its settings may need to allow fine-grained tokens first.

**2. Create a job on [cron-job.org](https://cron-job.org)** (free)

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/OWNER/REPO/actions/workflows/watch.yml/dispatches` |
| Schedule | every 5 minutes |
| Request method | `POST` |
| Header | `Authorization: Bearer YOUR_TOKEN` |
| Header | `Accept: application/vnd.github+json` |
| Header | `X-GitHub-Api-Version: 2022-11-28` |
| Request body | `{"ref":"main"}` |

A successful call returns **HTTP 204**. Runs then appear in the Actions tab as
`workflow_dispatch` every 5 minutes, and the heartbeat's
`N checks in the last 6h (expected ~72)` line should read close to 72.

The GitHub schedule stays in place as a backstop. Overlapping runs are
prevented by the workflow's `concurrency` group.

---

## Keep the repo public

GitHub Actions is **free and unlimited on public repositories**. On a private
repo the Free plan gives **2,000 minutes/month** and bills each job as at least a
minute; a 5-minute cadence is about 8,600 runs a month. Nothing sensitive is in
the repo (the topic lives in Secrets), so public is the simple choice.

GitHub also **disables scheduled workflows after 60 days without repo
activity**. The heartbeat commits state every few hours, which keeps the repo
active.

---

## Adding more products

Edit `config.json` and add an entry to `targets`.

**Nintendo store** (any region; paste the product page URL):

```json
{
  "id": "nintendo-ca-something",
  "enabled": true,
  "source": "nintendo",
  "label": "Human readable name",
  "url": "https://www.nintendo.com/en-ca/store/products/some-product-123456/"
}
```

Physical items end in a numeric SKU, which the checker uses to find the right
product on the page. Pages without one (digital games) fall back to schema.org.

**Best Buy CA** (the SKU is the number in the product URL):

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

**Amazon.ca / Walmart.ca / EB Games**: `"source"` plus the product `"url"`.

Set `"enabled": false` to mute a target without deleting it.

### Tuning discovery

`discovery.must_match_any` and `and_must_match_any` are substring filters against
the product URL's slug. A product must match **at least one entry from each
list**. `ignore_containing` excludes anything containing those substrings.
`region` is the store to watch, and `sitemaps` lists which sitemaps to scan.

---

## Diagnosing a blocked source

If a source starts failing, run the workflow with **diagnose** ticked. It
probes one target per HTML source with three fetch strategies and reports HTTP
status, redirects, final URL, page title and block markers, then commits the
result to `diagnostics.txt` so you can read it without opening the Actions logs.

Disabled targets are probed too. Locally:

```bash
python3 zelda_watch.py --diagnose
```

Reading the output: a redirect to a `/blocked` URL or a 403 means IP-reputation
blocking (not fixable client-side). A 200 with a small body and `no-NEXT_DATA`
means a JavaScript challenge. A 429 means you are being rate limited, so back off.

## Running it locally

Needs Python 3 (standard library only):

```bash
DRY_RUN=1 python3 zelda_watch.py
```

`DRY_RUN=1` checks everything and prints what it *would* send. Drop it and set
`NTFY_TOPIC` to actually push. `python3 zelda_watch.py --self-test` runs the
self-test.

---

## How it avoids spamming you, and failing silently

- Alerts fire **only on the transition** into buyable, not every run while
  stock lasts. One push per drop.
- The first discovery run records a silent baseline instead of alerting on every
  product that already exists.
- If a source breaks or gets blocked, the others keep working, and you get one
  low-priority heads-up after 6 consecutive failures.
- The **heartbeat** (every `heartbeat_hours`, default 6) lists every target, re-runs
  the self-test, and reports how many checks actually ran against how many were
  scheduled. Silence between heartbeats means "nothing in stock", not "the bot
  died".

---

## What went wrong on 10 September 2026

The console came back in stock on the Nintendo Canada store and no notification
was sent.

- **Detected but suppressed.** The run at 16:30 UTC read the console as
  `InStock`. An earlier version then applied a heuristic: if the page showed
  "Find retailers" but not "Add to cart", treat the item as not sold direct.
  "Add to cart" is never in the server HTML and "Find retailers" appears on
  directly sold items, so that rule suppressed essentially every Nintendo
  restock. It is gone, replaced by `isSalableQty`, and a regression test replays
  the exact page shape.
- **Checked too rarely.** GitHub ran the 5-minute schedule every 2–4 hours. This
  restock happened to land inside a run, but most would not.
  [Reliable scheduling](#reliable-scheduling) fixes that.
- **Missed listings.** The Zelda Pro Controllers and carrying case were live on
  the Canadian store but absent from its sitemap, so discovery never saw them.
  Discovery now cross-checks the US sitemap.
- **Why testing missed it.** The checker had only ever been shown to report
  out-of-stock items correctly. It was never pointed at an item known to be in
  stock, which would have exposed the bug on day one. The self-test now does
  exactly that, on demand and with every heartbeat.

---

## Being a good citizen

`nintendo.com/robots.txt` allows general crawling (`User-agent: *` / `Allow: /`).
A run fetches a handful of product pages and two sitemaps, comparable to
leaving a few tabs open. Please do not trigger runs more often than every 5
minutes; that is how IP ranges end up blocked for everyone.
