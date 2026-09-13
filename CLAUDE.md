# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A notify-only restock watcher. It checks product links at approved stores and
pushes an alert (through ntfy, and optionally Discord) when an item becomes
buyable, and it can spot new products on the Nintendo store. It runs on GitHub
Actions and never adds to cart or checks out; keep it that way. It was built to
catch the Switch 2 Zelda 40th Anniversary Edition in Canada, and everything
product-specific lives in `config.json`. `README.md` is the user-facing guide.

## Commands

Python 3, standard library only; there is nothing to install or build.

```bash
python3 -m unittest discover -s tests -v                 # all offline tests (about 90, ~6s)
python3 -m unittest discover -s tests -k test_nintendo    # tests whose name matches
python3 zelda_watch.py --check-config                     # validate config.json; makes no requests
python3 zelda_watch.py --check-link URL                   # what the watcher reads from one link (live)
DRY_RUN=1 python3 zelda_watch.py                          # full live check that prints instead of sending
python3 zelda_watch.py --self-test                        # known in-stock Nintendo items -> real push
python3 zelda_watch.py --diagnose                         # probe blocked stores; writes diagnostics.txt
```

Local runs refuse to start within 5 minutes of the previous one unless `--force`
is passed (this protects the owner's home IP). `DRY_RUN` still rewrites
`state.json`, so restore it after any local run with `git checkout -- state.json`.
Never commit local state.

**This machine (Windows):** there is no system Python. Use the Python bundled with
Unity:
`C:\Program Files\Unity\Hub\Editor\6000.5.3f1\Editor\Data\PlaybackEngines\WebGLSupport\BuildTools\Emscripten\python\python.exe`.
Machine quirks that have caused trouble:

- `con.*` is a reserved device name, so writes to it silently vanish.
- In PowerShell, `Remove-Item Env:\X` is blocked by the sandbox; use `$env:X = $null`.
- `git merge -F -` doesn't read stdin; use `git merge --no-commit`, then `git commit -F -`.
- Git Bash mangles `rev:path` arguments; prefix with `MSYS_NO_PATHCONV=1`.
- Windows curl lacks `--http2`.
- The Unity Python can't verify walmart.com's certificate; curl can.

## Deployment model

- **`main` is live.** cron-job.org POSTs a `workflow_dispatch` to
  `.github/workflows/watch.yml` every 5 minutes, using a fine-grained token
  (Actions read/write, this repo only). It expires around mid-December 2026.
  GitHub's own `schedule` trigger is only a backstop: in practice it ran every
  2–4 hours. Anything pushed to `main` runs within 5 minutes.
- The bot commits `state.json` after runs, with `[skip ci]`. Always
  `git pull --rebase` before pushing, and don't touch `state.json` in your own commits.
- `.github/workflows/tests.yml` runs the offline suite on every push and PR.
- Feature work goes on a branch and merges to `main` only with the owner's
  go-ahead. Urgent fixes to the live bot go straight to `main` once the tests pass.
- Secrets: `NTFY_TOPIC` (one shared topic) or `NTFY_TOPICS` (JSON, person → topic),
  and `DISCORD_WEBHOOK_URL`. Never log them, and never ask the user to paste them
  into chat. Discord role ids are not secret and live in `config.json`.
- The workflow's inputs `self_test`, `dry_run` and `diagnose` are checkboxes on
  **Run workflow**. A run started without ticking one is just an ordinary check.

## Architecture

Everything is in `zelda_watch.py`. A normal run, in `main()`:

1. **Config.** Load `config.json` (version 2), then `validate_config()`. Errors
   stop the run; per-link problems are warnings, and those links are skipped.
   One-off commands (`--check-config`, `--check-link`, `--diagnose`,
   `--self-test`) branch off here.
2. **Delivery setup.** `resolve_topics()` maps people to ntfy topics from the
   secrets. Discord is enabled only if the webhook secret looks like a Discord
   webhook. If it doesn't, the run warns and the heartbeat says so; ntfy is
   unaffected.
3. **State.** Load `state.json`, run `migrate_state()` (v1 targets → v2 links),
   and restore the per-host `cooldowns`.
4. **Discovery** (at most hourly, `discovery_due()`). Scan the Nintendo sitemaps
   for URLs matching the keywords, and probe our region for products listed only
   abroad; the CA sitemap lags the US one. New finds go into `auto_watch`.
5. **Watchlist.** `build_watchlist()` expands products into links. The store is
   detected by domain (`all_stores()` / `detect_store()`; custom stores win over
   built-ins). A link is skipped if paused, from an unknown store, from a store
   not in `approved_stores`, or from a store with `github_ok: False` when running
   on Actions.
6. **Check.** Each link goes to its store's checker, which returns
   `(buyable, detail)` or raises. `RateLimited` means the host is on cooldown.
   `BotChecked` pauses the store with an escalating back-off (6, 12, 24, then 48
   hours), tracked in `state["blocks"]`. Any other exception counts as a failure,
   and 6 in a row trigger one "Watcher problem" alert.
7. **Alerts.** A push is sent only on a transition into buyable, one per product
   per run: `stock_alert()` (titled IN STOCK, or PRE-ORDER OPEN when every detail
   contains "pre-order") or `listing_alert()` (NEW LISTING). Then the heartbeat
   (`heartbeat_hours`): per-link status, skipped links, a self-test re-run, and
   actual versus expected run counts.
8. **Send.** ntfy: one POST per distinct topic of the alert's people. Discord:
   `discord_payload()`, where `allowed_mentions` is locked to the product topic's
   role, so `@everyone` can never ping. Status alerts go to Discord only if
   `discord.status_alerts` is set. Then prune stale links, persist cooldowns, and
   save state.

Every request goes through `polite_wait()`, which spaces requests to one host
3 seconds apart and honours cooldowns. `fetch_with_fallbacks()` (browser
headers, then plain, then curl) is only for bot-protected stores, and it stops
at the first 429.

## Decisions that must not regress

- **Nintendo** stock comes from `isSalableQty` on the `__NEXT_DATA__` product node
  whose SKU matches the URL, cross-checked with schema.org. If the two disagree,
  it alerts. Never infer stock from "Add to cart" (rendered client-side) or
  "Find retailers" (shown on items Nintendo sells directly). That heuristic
  silenced a real restock on 2026-09-10.
- **Unreadable is not "sold out".** Checkers raise when they can't find a
  signal, so a broken store is visible instead of silently quiet.
- **Custom stores** (`check_generic`) try Shopify's `/products/<handle>.js`
  (variant-aware via `?variant=`), then the user's `in_stock_text` and
  `sold_out_text` (visible text only), then schema.org.
- **State is keyed by `link_key(url)`.** Changing a link's URL resets its history.
- **Walmart and EB Games** block GitHub's runner IPs (IP reputation, so no
  client-side trick fixes it). They're `github_ok: False` and are only checked
  from a home connection.

## Tests

All tests are offline. `tests/fixtures/` holds minimised copies of real pages,
one in-stock and one sold-out per store. Nintendo and Walmart pages include
related-product **decoys** with the opposite stock status, so the tests prove the
checker reads the product in the link. `tests/helpers.py` provides `make_cfg()`
and `WatcherTestCase.run_main()` (a sandboxed `main()` with a temp config and
state, captured pushes, and GitHub-like env). `test_watch.py`'s `FakeWeb`
replaces `z.fetch` and `z.fetch_via_curl` by URL. `test_politeness.py` and
`test_discord.py` use a throwaway local HTTP server.

The owner's rule (from CQ, who reviews this project): **prove the positive path
on a case known to be true before calling detection done**, e.g. an in-stock
fixture and the self-test, not only that sold-out reads as sold out. A new store
needs a checker, a `STORES` entry, both fixtures, and tests. Don't hit live
stores repeatedly from the owner's home IP; capture once and test against
fixtures.

## Status (2026-09-13)

- Live: Phase 1 (products as links, approved and custom stores, per-person
  alerts), Discord alerts (topic "Zelda 2026" → role `1548538546472751206` in the
  owner's test server; a friend receives the pings), and the bot-check back-off.
- **Amazon link paused** (`"enabled": false`, with a `_why` note). Amazon.ca began
  serving bot checks to GitHub's runners about an hour after the 5-minute schedule
  started. The owner's own browsing is unaffected. Re-enable one link after a few
  days and confirm with a dry run. A per-store check interval (e.g. Amazon every
  30 minutes) is a proposed mitigation.
- The Discord self-test post has not been confirmed yet. The last attempt ran as
  an ordinary check (no self-test box ticked).
- Not built yet:
  - adding products from Discord (the owner is holding off)
  - Phase 2: a home or phone runner for Walmart, a token-expiry heads-up, and a
    fixture refresh tool
  - Phase 3: a landing-page README. This needs the owner's decisions on a
    license, renaming the repo (which changes the cron-job.org URL), and making
    it a GitHub template.
- A future move to a larger Discord server run by CQ is deferred. It has its own
  rules and an existing StockRadar bot, so avoid duplicating its alerts.
