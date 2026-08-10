# Cardvault — TCG Collection Manager

Django + HTMX web app for managing TCG collections (Yu-Gi-Oh!, Digimon — more games addable
in the admin). Replaces the old Jupyter-notebook pipeline, now archived in `legacy/`.

## Why the rewrite

The notebook pipeline kept the whole collection in one JSON file per game and rewrote it
wholesale on every edit — with several documented paths to silent data loss. Cardvault stores
everything in SQLite with:

- **A single audited write path**: quantities only change via `collection/services.py`, and
  every change writes an append-only `QuantityChange` row in the same transaction. Undo is
  always available (inverse change, never deletion).
- **Append-only price history** (`PriceSnapshot`) with timestamps and currency — the legacy
  format overwrote a single mutable price in place.
- **Catalog / collection / prices separated**: 61k catalog rows no longer live in the same
  structure as the ~2k owned rows.
- **Backups**: `manage.py backup_db`, plus automatic pre-scrape backups.
- **Card metadata and art** from the public catalogs (YGOPRODeck, digimoncard.io), joined to
  Cardmarket printings so you can filter by attribute, type, level or archetype — and see
  what the card actually looks like.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py import_legacy       # one-shot import from data/ (idempotent)
.venv/bin/python manage.py runserver
```

Open http://127.0.0.1:8000/ and log in. The initial admin password from the automated setup
is in `var/initial-admin-password.txt` — change it with `manage.py changepassword teufik`
and delete that file.

## Commands

| Command | Purpose |
|---|---|
| `manage.py import_legacy [--game all\|yugioh\|digimon]` | Import the legacy JSON ledgers. Idempotent (re-runs are skipped; `--force` re-runs but never touches existing holdings); hard count-validation gate; never writes to `data/`. |
| `manage.py backup_db [--tag nightly]` | Safe live SQLite backup to `var/backups/` (keeps 14 regular + 10 pre-job). |
| `manage.py run_scrape_job <id>` | Execute one scrape job (normally spawned by the scrape panel; running it manually in a terminal behaves identically). |
| `manage.py warm_browser [--game X]` | Open the scraper's Chrome so you can clear Cloudflare's check by hand; the clearance is reused by later jobs. |
| `manage.py import_catalog [--game X] [--limit N]` | Pull card metadata + art URLs from YGOPRODeck / digimoncard.io. One HTTP request per game; never touches holdings, prices or printing links. |
| `manage.py resolve_printings [--game X] [--redo] [--no-fuzzy]` | Link Cardmarket printings to catalog cards. Never overwrites a manual resolution. |
| `pytest` | Run the test suite. |

## Managing the collection

- **Quantities** — open a set and use the −/+ steppers or type a number. Every edit saves
  instantly, writes an audit row, and offers Undo. Enter jumps to the next row (binder flow).
- **Bulk actions** — on a set page, "Set all / +1 to all / Zero all" applies to exactly the
  rows matching the current filter. You always see the precise impact first ("47 cards will
  change, 3 already at that value") and the whole batch undoes in one click.
- **Quick add** — press `/` anywhere: search any card across games, set its quantity inline.
  For loose cards, without hunting for the right set first.
- **Packages** — two kinds, one mechanism. A **sell package** bundles cards to sell together
  and freezes each price when you add it, so a moving market doesn't change the deal you
  quoted. A **keep package** is the opposite: its copies are never offered for sale and are
  subtracted from what any sell package may commit. (This replaced the separate keep-list;
  migration `collection.0004` moved existing reservations across and reverses cleanly.)
  "Mark sold" validates every line against what's actually sellable (owned minus reserved),
  then decrements holdings through the audited path — each change individually revertible.
- **Exports** — XLSX (hierarchical, matching the old notebook layout) and CSV (tidy, one row
  per card) from the dashboard or `/g/<game>/export.xlsx`. Filter with
  `?keepers=exclude|only&basis=trend&min_price=0.5`.

## Card catalog and art

Cardmarket tells you what a product costs; it does not tell you what the card *does*. Two
free public APIs fill that in:

| Game | Source | Identity |
|---|---|---|
| Yu-Gi-Oh! | `db.ygoprodeck.com/api/v7/cardinfo.php` | 8-digit passcode (+ alt-art aliases) |
| Digimon | `digimoncard.io/api-public/search.php` | set number, e.g. `BT10-040` |

```bash
manage.py import_catalog      # ~14,500 YuGiOh + ~4,400 Digimon cards, one request each
manage.py resolve_printings   # link Cardmarket printings to those cards
```

**Resolution** is the join that makes art, metadata and filters possible, and the two games
need opposite strategies:

- **Digimon — parse, don't match.** The set number is the identity and it is embedded in the
  slug (`Achillesmon-BT10-040`). Name matching would be hopeless: 3,336 of 4,387 Digimon
  cards share a name with another card.
- **Yu-Gi-Oh! — ordered exact attempts, then cautious fuzzy.** The untouched slug is always
  tried first, then rarity and `-V2` art suffixes are peeled alternately until it stops
  shrinking. Order is the safety property: real cards end in rarity words
  (`Junk-Collector`, `Elemental-HERO-Captain-Gold`) and would be mangled by stripping first.
  Fuzzy never auto-accepts below 96, and never resolves to a name shared by two cards.

Measured on the real collection: **99.6 % of YuGiOh printings and 100 % of Digimon**
(99.3 % / 99.9 % of cards actually owned). The rest are a review queue — sort
`/admin/catalog/printing/` by resolution status, or use the "mark as ignored" action.

A catalog refresh never re-links a printing a human resolved by hand.

### Images

Both providers forbid hotlinking — YGOPRODeck's guide says failure to re-host "will result in
an IP blacklist". So no template points at a remote image. `/card-art/<piece>/<size>/`
downloads on first view, stores under `var/card-images/` (sharded, atomic write,
host-allowlisted) and serves the local copy with a one-year immutable cache afterwards.
Roughly 200 ms cold, under 2 ms warm. Nothing is bulk-downloaded; art you never look at
never costs you disk. `CARD_IMAGE_DIR` moves the cache; deleting it is always safe.

## Scraping (admin-only)

Open **Scrape** in the nav (staff users only). Three job types:

- **Discover expansions** — finds new sets on Cardmarket (last N year groups). Minutes.
- **Discover cards** — fills one expansion (or all not-fully-scraped ones) with its card
  list. Additive only; existing cards and quantities are never touched. Sets over
  Cardmarket's 300-results cap are flagged; enable "deep search" to resolve them (slow).
- **Refresh prices** — re-scrapes From/Trend/30-day-avg for owned cards. Default scope is
  **stale only** (older than 30 days). Every observation is appended to price history.
  ≈7–10 s per card, so a full refresh of ~3,100 cards runs for hours.

**Settings** (scrape panel, or `/admin/scraping/scrapersettings/`): the **Chrome executable**
path — blank uses the platform default, and on macOS it must be the binary *inside* the app
bundle, not the `.app` folder — plus the Cloudflare wait and the min/max pause between items.
Slower is safer: a long sweep at 10–20 s per item costs hours, but a block costs a day.

Operational notes:

- A **DB backup is taken automatically before every job**; a job cannot start without one.
- Jobs run in a detached process and survive closing the browser. Progress polls live on
  the job page; **Cancel** is always safe — every card already fetched is committed.
- Only one job can run at a time (one Selenium session).
- A visible Chrome window opens while a job runs (headless Chrome gets blocked). For
  long runs, keep the Mac awake: `caffeinate -i` in a terminal, or plug it in and disable
  sleep. If the run dies anyway, just start the same job again — stale-only scope and
  per-item checkpoints make re-runs cheap.
- Cardmarket changes its HTML occasionally. Failures land in the job log and the
  failed-items list — they never corrupt data.

### Cloudflare

Cardmarket sits behind Cloudflare bot protection. The decisive finding (2026-08-09,
measured on one IP within the same minute, same URL):

| how Chrome was started | result |
| --- | --- |
| normally, like a person | challenge auto-cleared in ~5 s, real page |
| by chromedriver | `Sorry, you have been blocked` (403) |

**It is chromedriver *launching* Chrome that gets refused** — not this machine, not
Selenium, not the User-Agent. chromedriver starts Chrome with its own switches and a
throwaway profile, and that launch signature is what Cloudflare scores. So:

- **The scraper starts Chrome itself** (path configurable on the scrape panel) on the game's
  front page,
  lets it pass the check like any browser, and only then attaches Selenium over the
  debugging port. Driving an already-cleared browser is fine.
- **The profile persists** in `var/chrome-profile/`, so clearance survives between drivers
  and jobs. One browser serves a whole run (recycled every 25 items). Delete the directory
  to start clean.
- **Leave the Chrome window visible.** If a challenge does show a checkbox, click it — the
  job waits (configurable, default 180 s) then carries on. Headless gets
  challenged far more aggressively; don't. `manage.py warm_browser --game yugioh` seeds
  the clearance by hand before a long run.
- **A hard block stops the run**, with a `Cardmarket is blocking the scraper …` message
  rather than a traceback. Nothing is ever marked scraped from a blocked page, no set is
  blamed for it, and remaining items stay pending. Wait 15–60 minutes before retrying.
- Sets Cardmarket legitimately lists with **no singles** complete with zero cards; that's
  distinct from a block and always has been (the legacy notebook checked for it too).

### Listing markup

Cardmarket replaced the singles **table** with a **card gallery** at some point before
2026-08-09; `.table-body`, `.col-10` and `.col-price` no longer exist. A tile is
`a.galleryBox`, carrying the product href, the name, the set code (`DP08-JP`) and
`From <b>6,00 €</b>`. 30 tiles per page, and pagination stops at 10 pages — reaching
page 10 *is* the old "300+" cap, which no longer prints a marker of its own.

### Nightly backups (macOS launchd)

Save as `~/Library/LaunchAgents/com.cardvault.backup.plist`, then
`launchctl load ~/Library/LaunchAgents/com.cardvault.backup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.cardvault.backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/teufik/card-market-pricer/.venv/bin/python</string>
    <string>/Users/teufik/card-market-pricer/manage.py</string>
    <string>backup_db</string>
    <string>--tag</string><string>nightly</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/teufik/card-market-pricer</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
</dict>
</plist>
```

## Data layout

- `var/db.sqlite3` — the database (gitignored). WAL mode; web + scraper can share it.
- `var/backups/` — timestamped backups.
- `data/` — the **legacy JSON ledgers, read-only archive**. Nothing writes here anymore.
- `legacy/` — the retired notebooks and analyzer scripts, kept for reference.
- `credentials/` — Google service-account key (gitignored). **The old key was once committed
  and must be rotated in the GCP console** (project `card-market-pricer`); local git history
  has been scrubbed, but rotation is the real fix.

## Roadmap (from the approved plan)

1. ✅ Foundation: models, legacy import (totals verified to the cent vs the last XLSX),
   read-only browsing, quantity editing with audit + undo.
2. Bulk actions, quick-add, sell packages, keeper management UI, XLSX/CSV exports.
3. Scraper port (admin-only background jobs, per-card checkpoints, pre-job backups).
4. Want-list analyzer (replaces `legacy/analyze_buyer_*.py`, exact matching only).
5. Deck maker: catalog import (YGOPRODeck / digimoncard.io), slug→canonical resolution,
   .ydk decklist import, "you can build X% of this deck for €Y".
6. Google Sheets push, polish, deployment prep.

> **Future-product note:** redistributing scraped Cardmarket prices to other users is a
> ToS/legal risk. A public product needs licensed price data or per-user data sources.
> Personal use stays personal.
