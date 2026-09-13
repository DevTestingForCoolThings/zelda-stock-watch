# Restock Watcher

Pushes a notification to your phone the moment something you want comes back in
stock: at Nintendo, Best Buy Canada, Amazon, Walmart, EB Games, or any shop you
add yourself. It can also tell you when a new product first appears on the
Nintendo store.

Built to catch the **Nintendo Switch 2 – The Legend of Zelda 40th Anniversary
Edition** in Canada. Everything specific to that lives in `config.json`, so
you can point it at anything.

- **Runs on GitHub Actions**, so your computer doesn't need to be on.
- **No dependencies** (Python standard library only), and **no accounts** beyond
  GitHub. Notifications go through [ntfy](https://ntfy.sh), which needs no sign-up.
- **Notify-only.** It never adds to cart or checks out. You get a push with a
  tappable link and you buy it yourself.

---

## Supported stores

| Store id | Store | How it's read | Works from GitHub |
|---|---|---|:---:|
| `nintendo` | Nintendo Store (any region) | `isSalableQty` on the product, cross-checked with schema.org | ✅ |
| `bestbuy-ca` | Best Buy Canada | Best Buy's public stock API | ✅ |
| `amazon` | Amazon (.ca and .com) | add-to-cart and out-of-stock markers | ✅ |
| `walmart` | Walmart (.ca and .com) | the product's own data on the page | 🏠 home only |
| `ebgames` | EB Games Canada | schema.org | 🏠 home only |
| *your own* | any other shop | Shopify data, words you choose, or schema.org | ✅ usually |

🏠 **Walmart and EB Games block GitHub's servers.** Their links are skipped
when the watcher runs on GitHub and checked when it runs on a home connection.
The heartbeat tells you how many links were skipped and why.

---

## Quick start

1. **Get the ntfy app** ([iOS](https://apps.apple.com/app/ntfy/id1625396347) ·
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)), tap
   **+**, and subscribe to a long random topic name such as
   `restock-x7k2m9qp4v`. **The topic works like a password**: anyone who knows it
   can read your alerts.
2. **Copy this repo** (fork it, or create a new public repo from it). Keep it
   **public**: GitHub Actions is free and unlimited on public repos, and nothing
   sensitive is stored in it.
3. **Add your topic as a secret**: repo **Settings → Secrets and variables →
   Actions → New repository secret**, name `NTFY_TOPIC`, value = the topic name
   only. Use the **Secrets** tab, not Variables.
4. **Write your `config.json`** by copying `config.example.json` and pasting in
   your product links (see below). Check it with
   `python3 zelda_watch.py --check-config`.
5. **Run the self-test**: **Actions → Zelda 40th stock watch → Run workflow**,
   tick **Self-test**. Your phone should show
   **SELF-TEST IN STOCK: Nintendo Switch 2 Pro Controller** within a minute.
6. **Set up [reliable scheduling](#reliable-scheduling).** Don't skip this: on its
   own, GitHub runs the checks only every few hours.

---

## Adding products

A product is a name plus its link at every store you'd buy it from. Copy the
links straight from your browser; the watcher works out which store each one
belongs to.

```json
"products": [
  {
    "name": "Zelda 40th Pro Controller",
    "links": [
      "https://www.nintendo.com/en-ca/store/products/nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition-127074/",
      "https://www.bestbuy.ca/en-ca/product/20149830",
      "https://www.walmart.ca/en/ip/Nintendo-Switch-2-Pro-Controller-The-Legend-of-Zelda-40th-Anniversary-Edition/3CVEAW67GHIT"
    ]
  }
]
```

- **`notify`** (optional): who gets this product's alerts, e.g. `["julie"]`.
  Defaults to everyone. See [Sharing with a friend](#sharing-with-a-friend).
- **Pause** a single link with `{"url": "...", "enabled": false}`, or a whole
  product with `"enabled": false`.
- Best Buy links must end in the product number (the SKU), as copied from the
  address bar.

### Approved stores

`approved_stores` lists the stores you're willing to buy from. A link to any
other store is skipped. That makes it a quick way to rule a store in or out
without editing every product.

```json
"approved_stores": ["nintendo", "bestbuy-ca", "amazon", "walmart"]
```

After editing, run `python3 zelda_watch.py --check-config`. It lists every
product and link with its status (`ok`, `home connection only`,
`SKIPPED - not approved`, and so on), and explains any mistake in plain words.
It makes no requests.

---

## Adding your own store

For niche products at shops that aren't built in:

**1. See whether the watcher can read it.** Paste a product link:

```bash
python3 zelda_watch.py --check-link "https://www.limitedrungames.com/products/r-type-dx-cd-soundtrack-limited-run-games-edition"
```

It shows the store it detected, what it read, and whether it's buyable. For a
shop it doesn't know yet, it tries the custom-store reader and tells you the
exact entry to add.

**2. Add the shop to `custom_stores`, and its id to `approved_stores`:**

```json
"custom_stores": [
  {
    "id": "limited-run-games",
    "name": "Limited Run Games",
    "domains": ["limitedrungames.com"]
  }
],
"approved_stores": ["nintendo", "limited-run-games"]
```

That's usually all. The custom-store reader tries, in order:

1. **Shopify's product data.** A large share of small shops run on Shopify. This
   is precise, and understands sizes and colours: a link ending in
   `?variant=12345` watches that exact variant.
2. **Words you tell it to look for**, if you add them:
   `"sold_out_text": ["Sold out"]` and/or `"in_stock_text": ["Add to cart"]`.
   Only visible page text counts, not scripts.
3. **schema.org stock data**, which WooCommerce, BigCommerce and most large
   retailers publish.

If none of these works, the link shows as *not working* rather than silently
reading "sold out" forever. To force one method, add
`"method": "shopify"`, `"schema"` or `"text"`. If a shop blocks GitHub, add
`"github_ok": false` and it will only be checked from home.

---

## Sharing with a friend

Several people can share one watcher, and each gets only the products they care about.

1. Your friend installs ntfy and picks **their own** topic, then sends it to you
   privately.
2. Add them to `notifications.people`:

   ```json
   "notifications": {
     "people": ["julie", "sam"],
     "status_alerts_to": ["julie"]
   }
   ```

3. Replace the `NTFY_TOPIC` secret with one called **`NTFY_TOPICS`** that holds
   everyone's topics as JSON:

   ```json
   {"julie": "julies-topic", "sam": "sams-topic"}
   ```

4. On each product, `"notify": ["sam"]` means only Sam, `["julie"]` means only
   you, and leaving it out means everyone.

`status_alerts_to` decides who gets the heartbeat and the "watcher problem"
warnings. Usually that's just whoever looks after the repo. Topics are never
stored in the repo or printed in the logs.

*The quick way:* your friend subscribes to your topic in the ntfy app. They'll get
exactly what you get, heartbeats included.

---

## What the alerts mean

| Title | Meaning |
|---|---|
| **IN STOCK:** *product* | Buyable now. Lists every store where it just became available, in one push. |
| **PRE-ORDER OPEN:** *product* | Buyable now as a pre-order; the push says when it ships. |
| **NEW LISTING:** *product* | A new page appeared on the Nintendo store. It says whether it's buyable yet; if not, you'll get IN STOCK later. |
| **Watcher still alive** | Quiet all-clear every `heartbeat_hours`: every link's status, anything skipped, the self-test result, and how many checks actually ran. |
| **Watcher problem:** *link* | One link has failed 6 checks in a row (blocked, or the page changed). Everything else keeps running. |
| **SELF-TEST …** | The result of a self-test you ran, or one repeated by the heartbeat. |

You get **one push per product per drop**, never one every five minutes while stock lasts.

---

## New-product discovery (Nintendo)

The `discovery` block scans the Nintendo store's sitemaps each hour for product
pages whose address contains your keywords (at least one from
`must_match_any` **and** one from `and_must_match_any`). Anything new triggers
a **NEW LISTING** push. With `auto_watch`, the product is then stock-watched
automatically.

List more than one region's sitemap if you can. The Canadian sitemap lags: in
September 2026 the Zelda Pro Controllers were on the Canadian store for days
before appearing in it, while the US sitemap already listed them. Pages found
only in another region's sitemap are checked on your region directly.

---

## Reliable scheduling

The workflow asks GitHub to run every 5 minutes. **In practice GitHub ran it only
every 2 to 4 hours** (14 runs in 46 hours, September 2026), and a restock can
come and go in between.

The fix is to trigger the workflow from outside on a real timer.

**1. Create a narrowly scoped GitHub token.** Avatar → **Settings → Developer
settings → Personal access tokens → Fine-grained tokens → Generate new token**.

- **Resource owner**: whoever owns the repo
- **Repository access**: *Only select repositories* → this repo
- **Permissions → Repository → Actions**: *Read and write*
- **Expiration**: pick a date and set yourself a reminder

The token can start this one workflow and nothing else. If an organisation owns
the repo, it may need to allow fine-grained tokens first.

**2. Create a job on [cron-job.org](https://cron-job.org)** (free):

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/OWNER/REPO/actions/workflows/watch.yml/dispatches` |
| Schedule | every 5 minutes |
| Request method | `POST` |
| Header | `Authorization: Bearer YOUR_TOKEN` |
| Header | `Accept: application/vnd.github+json` |
| Header | `X-GitHub-Api-Version: 2022-11-28` |
| Request body | `{"ref":"main"}` |

A test run should return **HTTP 204**. The heartbeat's
`N checks in the last 6h (expected ~72)` line then confirms it's working.

---

## Commands

| Command | What it does |
|---|---|
| `python3 zelda_watch.py --check-config` | Validate `config.json` and list what will be watched. No requests. |
| `python3 zelda_watch.py --check-link URL` | Show exactly what the watcher reads from one link. |
| `python3 zelda_watch.py --self-test` | Check known in-stock Nintendo items and send a real test push. |
| `python3 zelda_watch.py --diagnose` | Probe blocked stores with several fetch methods; writes `diagnostics.txt`. |
| `DRY_RUN=1 python3 zelda_watch.py` | A full check that prints what it would send instead of sending it. |

In the Actions tab, **Run workflow** has checkboxes for the self-test, a dry run,
and diagnostics.

**Running on your own computer** checks Walmart and EB Games too. To protect a
home connection, a second run within 5 minutes is refused unless you add
`--force`.

---

## Being a good citizen (and not getting blocked)

- **Request spacing**: at least `politeness.request_spacing_seconds` (default 3)
  between requests to the same site.
- **Rate limits are obeyed.** On `429 Too Many Requests` (or `503` with
  `Retry-After`), the watcher leaves that site alone for as long as it asked, or
  `politeness.default_cooldown_minutes` (default 30), and remembers this across
  runs. It never retries with a different client, because ignoring a rate limit
  is how a slowdown becomes a block. A paused site shows as paused, not broken.
- **Discovery runs hourly**, not on every check (`discovery.every_minutes`),
  because it downloads large sitemaps.
- `nintendo.com/robots.txt` allows general crawling.

Please don't trigger runs more often than every 5 minutes.

---

## How it avoids spamming you, and failing silently

- Alerts fire only when something **becomes** buyable, and only once per product
  per drop.
- Discovery's first run records a silent baseline instead of alerting on every
  product that already exists.
- A broken link gets one low-priority warning after 6 failures, and everything
  else keeps running.
- The **heartbeat** lists every link, repeats the self-test, and reports how many
  checks really ran. Silence between heartbeats means "nothing in stock", not
  "the watcher died".
- **Tests**: `python3 -m unittest discover -s tests -v` runs about 70 offline checks
  against saved copies of real store pages (`tests/fixtures`), each store both
  in stock and sold out. They run automatically on every change and never
  contact a store.

---

## Upgrading from the first version

The first version used a `targets` list in `config.json`. The current format
(`"version": 2`) uses `products`, each with a list of links, plus
`notifications`, `approved_stores` and `custom_stores`. Rewrite the config
following `config.example.json` and run `--check-config`.

To carry over which items were already in stock, so the switch doesn't send the
same alerts again, add a one-time `migrate_from_v1` map of old target ids to
links. `config.json` in this repo shows an example. Delete it after the first
run.

---

## What went wrong on 10 September 2026

The Zelda console came back in stock on the Nintendo Canada store, and no
notification was sent.

- **Detected but suppressed.** The run at 16:30 UTC read the console as `InStock`,
  then an early heuristic overruled it: "shows *Find retailers* but not *Add to
  cart*, so Nintendo isn't selling it." In fact *Add to cart* is never in the page
  the server sends, and *Find retailers* appears next to it on items Nintendo
  does sell. The rule is gone, and a regression test replays that exact page.
- **Checked too rarely.** GitHub ran the 5-minute schedule every 2 to 4 hours.
  Fixed by [reliable scheduling](#reliable-scheduling).
- **Missed listings.** New Zelda pages were live but absent from the Canadian
  sitemap. Discovery now cross-checks other regions.
- **Why testing missed it.** The checker had only been shown to read *sold-out*
  items correctly, and never pointed at an item known to be in stock. The
  self-test now does exactly that, and every store in the test suite is proven
  both ways.
