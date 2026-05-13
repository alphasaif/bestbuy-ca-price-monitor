# BestBuy Canada Price Monitor

Python + Playwright scraper that reads in-stock phone listings from a
Supabase database, looks each one up on bestbuy.ca, and writes the lowest
observed price back as a new `bestbuy_prices` row (plus upserts into
`bestbuy_listings` and `assurant_listing_matches`).

Runs on GitHub Actions twice daily. Adapted from a Google-Sheets-based
predecessor; same matcher, same anti-detection, same retry logic — Supabase
I/O instead of Sheets.

## Architecture

```
assurant_listings ── filter (accepted grades, phone, in stock, unmatched)
        │
        └── scraper ─ bestbuy.ca search + product page ─ matcher.py
                │
                └── bestbuy_listings    (upsert by product_url)
                    bestbuy_prices      (insert — buy_box_price_cad, competing_offers)
                    assurant_listing_matches (upsert by assurant_listing_id)
                    agent_decisions     (insert on scrape failure)
```

## Setup (local)

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
# Fill in SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
```

### Env vars

| Name | Required | Default | Notes |
|---|---|---|---|
| `SUPABASE_URL` | yes | — | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | — | Service-role key. Never log. |
| `BESTBUY_PRICE_TTL_HOURS` | no | `24` | TTL filter applied only when `--force` is passed |

## CLI

```bash
python scraper.py [--dry-run] [--limit N] [--force] [--concurrency N] [--listing-id ID]
```

- `--dry-run` — prints which listings would be scraped, doesn't open any
  browser or write to Supabase. Useful to verify the filter query.
- `--limit N` — process only the first N matching rows. Order is whatever
  PostgREST returns (no explicit ORDER BY); fine for spot tests. Batch mode only.
- `--force` — operator override: include matched listings as well as
  unmatched ones. Skips listings whose most-recent `bestbuy_prices.fetched_at`
  is newer than `BESTBUY_PRICE_TTL_HOURS` (24h default).
- `--concurrency N` — override parallel browsers (default 5).
- `--listing-id ID` — single-listing dispatch mode. Process exactly that one
  `assurant_listings.listing_id`, bypassing **all** eligibility filtering
  (in-stock check, grade, match presence). Used to refresh one listing on
  demand. Wins over `--force` and `--limit`; a warning is logged if they're
  combined.

## Modes

| Mode | When | What runs | TTL applied? |
|---|---|---|---|
| `unmatched_only` (default) | Cron + manual run with no flags | Listings with NO BB match (`bestbuy_listing_id IS NULL`) | No |
| `force` | `--force` flag | All base candidates (matched + unmatched) | Yes (24h) |
| `single` | `--listing-id ID` | Exactly that one listing | No |

### Default mode — only-unmatched

Cron runs (and unflagged manual runs) only process listings that have **no
existing BestBuy match**. Matched listings are skipped regardless of how old
their last `bestbuy_prices` row is — they age indefinitely until refreshed.

To refresh a stale matched listing's price, dispatch this workflow with
`listing_id=<id>`. The single-listing path scrapes that one row and writes
the new price to `bestbuy_prices`.

### Base filter (always applied in batch modes)

- `available_quantity > 0`
- `grade IN ('DLS A+','DLS A-','DLS B+','DLS B-')`
- `LOWER(category) IN ('phone','phones','cell phone','smartphone')`
  **OR** `make IN ('Apple','Samsung','Google','Motorola','OnePlus','Nothing')`

Other grades (DLS A, DLS B, DLS C, DLS C-) are intentionally skipped — they
don't map to a BestBuy condition tier the matcher recognises.

Single-listing mode (`--listing-id`) bypasses this filter; you may
deliberately pick an out-of-stock or non-biddable row.

### Grade mapping (matcher hard-fail)

| Upstream grade | Matches BestBuy listings containing |
|---|---|
| `DLS A+`, `DLS A-` | "Refurbished (Excellent)" **or** "Excellent" |
| `DLS B+`, `DLS B-` | "Refurbished (Good)" **or** "Good" |

Hardcoded in `matcher.py` (`DLS_TO_MATCHER_GRADE`).

### Cache-first URL lookup

If `assurant_listing_matches` already has a BestBuy URL for a listing, the
scraper fetches that product page directly instead of re-searching. If the
cached page returns no content / no price / a product whose name no longer
passes the matcher's score threshold, it falls back to the full search.

## GitHub Actions

### Secrets required

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

### Schedule

Cron: `0 10,22 * * *` — 6 AM and 6 PM Eastern during DST (one hour later in
winter; drift is acceptable).

### Manual trigger

Actions → *BestBuy Canada Price Monitor* → **Run workflow**. Optional inputs:

- `listing_id` — single-listing dispatch (overrides everything else if set)
- `limit` — cap batch size
- `force` — include matched listings (subject to TTL)
- `concurrency` — parallel browsers
- `dry_run` — log only

### Artifacts

Each run uploads:
- `results-<run_id>` — `results/prices_YYYY-MM-DD.csv` (CSV backup)
- `logs-<run_id>` — `logs/scraper_YYYY-MM-DD.log`, `logs/errors.log`

Retained 14 days.

### Run summary

At the end of every run, the scraper prints:

```
RUN SUMMARY mode=<mode> attempted=N matched=M unmatched=K errors=E duration_seconds=D
::notice::BestBuy scrape RUN SUMMARY ... match_rate=XX.XX%
```

`mode` is one of `unmatched_only` / `force` / `single`.

For batch modes, if `matched/attempted < 0.8` the second line becomes
`::warning::` so the failure surfaces loudly in the GitHub Actions UI.
Single-listing runs always emit `::notice::` — a single mismatch isn't a
fleet-wide signal. No DB write for the summary.

## Operations

### A run failed — what now?

1. **Check the run's `logs-<run_id>` artifact.** `errors.log` shows only
   ERROR-level entries; `scraper_YYYY-MM-DD.log` has full per-row trace.
2. **Common causes:**
   - **Supabase auth**: "SUPABASE_URL and ... must be set" → service-role
     secret missing / rotated.
   - **Low match rate (`::warning::`)**: BestBuy likely changed selectors,
     or we hit a rate-limit. Re-run manually with lower `--concurrency`
     (e.g. `2`) and inspect `logs/scraper_*.log` for which rows failed.
   - **Playwright timeout**: transient. The scraper retries 3× with
     exponential backoff; a whole-run timeout means BestBuy is slow or
     blocking us. Check if one-off or sustained.
3. **Per-row failure audit**: every failed row writes an `agent_decisions`
   row with `decision='bb_scrape_failed'` and context in `inputs_json`.
   Query in Supabase:
   ```sql
   SELECT al.item_id, al.make, al.model, ad.reasoning, ad.inputs_json, ad.created_at
   FROM agent_decisions ad
   JOIN assurant_listings al ON al.listing_id = ad.assurant_listing_id
   WHERE ad.decision = 'bb_scrape_failed'
   ORDER BY ad.created_at DESC
   LIMIT 50;
   ```

### Refresh a single listing

- **Locally:** `python scraper.py --listing-id 1234`
- **In CI:** Actions → Run workflow → enter the `listing_id`.

### Force-rescrape matched listings in bulk

Default mode skips matched listings. To re-run prices on everything (matched
and unmatched), subject only to the 24h TTL:

- **One-off local rescrape:** `python scraper.py --force --limit 20`
- **In CI:** Actions → Run workflow → check **force**.

### Disable the cron

Edit `.github/workflows/scrape.yml`, comment out the `schedule:` block, commit
to main. (Or disable the workflow entirely via Actions → ⋯ → *Disable
workflow*.) `workflow_dispatch` will still work for manual runs.

### Skip a specific listing

No per-listing skip flag. The TTL naturally handles rate-limiting rescrapes.
To force an entry out of circulation, either set
`assurant_listings.available_quantity = 0` or adjust the filter in
`supabase_io.read_biddable_listings_needing_bb_prices`.

## File tour

- `scraper.py` — entry point, Playwright orchestration, CSV backup, run summary
- `matcher.py` — query construction + fuzzy scoring
- `supabase_io.py` — Supabase read/write layer
- `requirements.txt` — pinned minimums for `supabase`, `playwright`, `fuzzywuzzy`
- `.github/workflows/scrape.yml` — CI cron + manual trigger
- `.env.example` — required env vars

## License

No license declared. All rights reserved. Open an issue if you'd like to
reuse this for a different purpose.
