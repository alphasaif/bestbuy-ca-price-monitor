# BestBuy Canada Price Monitor

Python scraper that reads in-stock phone listings from a Supabase database,
looks each one up on bestbuy.ca via two unauthenticated JSON endpoints, and
writes the resulting price to a new `bestbuy_prices` row (plus upserts
`bestbuy_listings` and `assurant_listing_matches`).

Designed to run on GitHub Actions on a 12-hour cron, plus on-demand via
`workflow_dispatch` for single-listing refreshes.

## How it works

Two HTTP calls per listing — no browser, no DOM scraping, no proxy:

```
For each listing in assurant_listings (filtered):
  If listing has a cached BestBuy URL with parseable SKU:
    GET Best Buy Canada's product-detail JSON endpoint by SKU
    → verify returned name still passes matcher's grade hard-fail
    → write to bestbuy_prices
  Else (or cache verification failed):
    GET Best Buy Canada's keyword-search JSON endpoint with the listing's
        brand + model + capacity + color query
    → matcher.pick_best_match against the returned product list
    → write to bestbuy_prices straight from the matched search-row fields
```

The Dashboard reads `bestbuy_prices.competing_offers` for downstream
decisions. The shape preserves the historical keys `main_price`,
`marketplace_prices`, and `matched_name` for backward compatibility, plus a
broader set of new keys (`sku`, `is_marketplace`, `is_available`,
`is_purchasable`, `is_online_only`, `is_clearance`, `regular_price`,
`sale_price`, `is_on_sale`, `seller_id`, `model_number`, `brand_name`,
`api_source`).

### Capability note

The old DOM-scraping pipeline (Playwright) could capture multiple competing
marketplace seller prices for a single product (the "more sellers" widget on
each product page). The JSON pipeline returns one product record per search
hit — whichever seller matched — so `marketplace_prices` is now always an
empty array. Per-product offer enumeration is not available through these
endpoints. This is a deliberate capability trade-off, not a regression bug.

### Canary check

Every run starts with a smoke test against a known SKU. If the canary fails
(network error, unexpected response shape, wrong SKU in the body), the
process exits with code 2 before any Supabase reads or writes occur. This
fails the run fast if Best Buy ever changes the endpoint's behaviour.

## Setup (local)

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
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
python scraper.py [--dry-run] [--limit N] [--force] [--unmatched-only] [--concurrency N] [--listing-id ID]
```

- `--dry-run` — prints which listings would be scraped, doesn't hit Best Buy
  or write to Supabase. Useful to verify the filter query.
- `--limit N` — process only the first N matching rows. Batch mode only.
- `--force` — operator override: include matched listings as well as
  unmatched ones. The TTL filter still applies.
- `--unmatched-only` — explicit alias for the default cron behaviour. No-op
  when neither `--force` nor `--listing-id` is set; useful for making cron
  invocations self-documenting.
- `--concurrency N` — override max concurrent HTTP requests (default 10).
- `--listing-id ID` — single-listing dispatch mode. Process exactly that one
  `assurant_listings.listing_id`, bypassing **all** eligibility filtering
  (in-stock check, grade, match presence). Used to refresh one listing on
  demand. Wins over `--force` and `--limit`.

## Modes

| Mode | When | What runs | TTL applied? |
|---|---|---|---|
| `unmatched_only` (default) | Cron + manual run with no flags | Listings with NO BB match (`bestbuy_listing_id IS NULL`) | No |
| `force` | `--force` flag | All base candidates (matched + unmatched) | Yes (24h) |
| `single` | `--listing-id ID` | Exactly that one listing | No |

### Default mode — only unmatched

Cron runs (and unflagged manual runs) only process listings that have **no
existing BestBuy match**. Matched listings are skipped regardless of price
age — they age indefinitely until manually refreshed.

To refresh a stale matched listing, dispatch the workflow with `listing_id`
set, or run `python scraper.py --listing-id <id>` locally.

### Base filter (always applied in batch modes)

- `available_quantity > 0`
- `grade IN ('DLS A+','DLS A-','DLS B+','DLS B-')`
- `LOWER(category) IN ('phone','phones','cell phone','smartphone')`
  **OR** `make IN ('Apple','Samsung','Google','Motorola','OnePlus','Nothing')`

Other grades (DLS A, DLS B, DLS C, DLS C-) are intentionally skipped — they
don't map to a BestBuy condition tier the matcher recognises.

Single-listing mode (`--listing-id`) bypasses this filter; you may
deliberately refresh an out-of-stock or non-biddable row.

### Grade mapping (matcher hard-fail)

| Upstream grade | Matches BestBuy listings containing |
|---|---|
| `DLS A+`, `DLS A-` | "Refurbished (Excellent)" **or** "Excellent" |
| `DLS B+`, `DLS B-` | "Refurbished (Good)" **or** "Good" |

Hardcoded in `matcher.py` (`DLS_TO_MATCHER_GRADE`).

### Cache-first lookup

If `assurant_listing_matches` already has a BestBuy URL for a listing, the
scraper extracts the trailing SKU from that URL and fetches the product
detail directly. The returned name is fed through the matcher's grade
hard-fail to catch silent condition-tier drift (e.g. a listing that was
once "Refurbished (Good)" but is now "Open Box" under the same SKU — rare
but possible). On verification miss, the scraper falls through to the
search path.

## GitHub Actions

### Secrets required

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

### Schedule

Cron declaration (commented out by default — re-enable after a few manual
runs verify cleanly):

```yaml
# - cron: "0 10,22 * * *"   # 6 AM / 6 PM Eastern during DST
```

### Manual trigger

Actions → *BestBuy Canada Price Monitor* → **Run workflow**. Optional inputs:

- `listing_id` — single-listing dispatch (overrides everything else if set)
- `limit` — cap batch size
- `force` — include matched listings (subject to TTL)
- `unmatched_only` — explicit alias for default mode
- `concurrency` — parallel HTTP requests (default 10)
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
   - **Canary failure** → exit code 2, no Supabase reads/writes happen.
     Best Buy's endpoint changed or the network can't reach it. Read the
     full canary log line to see whether the failure was HTTP, JSON shape,
     or SKU mismatch.
   - **Supabase auth** → "SUPABASE_URL and ... must be set". Service-role
     secret missing or rotated.
   - **Low match rate (`::warning::`)** → matcher rejected most candidates.
     Inspect `logs/scraper_*.log` for the per-row scores; usually means a
     listing's brand/model/grade combination doesn't have a BB equivalent.
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
- **In CI:** Actions → Run workflow → enter `listing_id`.

### Force-rescrape matched listings in bulk

```
python scraper.py --force --limit 20
```

Or in CI: Actions → Run workflow → check **force**.

### Disable the cron

Edit `.github/workflows/scrape.yml`, ensure the `schedule:` block is
commented out, commit to main. Manual `workflow_dispatch` still works.

### Skip a specific listing

No per-listing skip flag. To force an entry out of circulation, set
`assurant_listings.available_quantity = 0` or adjust the filter in
`supabase_io.read_biddable_listings_needing_bb_prices`.

## File tour

- `scraper.py` — HTTP layer, row orchestration, CSV backup, run summary
- `matcher.py` — query construction + fuzzy scoring (unchanged)
- `supabase_io.py` — Supabase read/write layer (one additive parameter; otherwise unchanged)
- `requirements.txt` — `curl-cffi`, `supabase`, `fuzzywuzzy`, `python-dotenv`
- `.github/workflows/scrape.yml` — CI cron (disabled) + manual trigger
- `.env.example` — required env vars

## License

No license declared. All rights reserved. Open an issue if you'd like to
reuse this for a different purpose.
