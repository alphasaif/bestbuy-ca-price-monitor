"""
scraper.py — BestBuy Canada price scraper, JSON-API edition.

Reads in-stock phone listings from Supabase, looks each one up on
bestbuy.ca via two undocumented (but unauthenticated and ungated)
JSON endpoints, picks the best match through matcher.py, and writes
the result to bestbuy_listings / bestbuy_prices / assurant_listing_matches.

Two HTTP routes per row:

  Cache-first  — if a cached BestBuy URL exists in assurant_listing_matches,
                 extract its SKU and call the product-detail JSON endpoint.
                 Verify the returned product name still passes the matcher's
                 grade hard-fail before writing.
  Search       — otherwise (or on cache verification miss), call the search
                 JSON endpoint, build Candidates from the result list, and
                 hand them to matcher.pick_best_match().

The previous Playwright pipeline was retired 2026-05-13 after Akamai
fully IP-blocked all three GitHub Actions runner OS pools. The JSON
endpoints used here are not bot-gated — they return the same data to
any User-Agent from any residential or datacenter IP — so we no longer
need a browser, anti-detection layer, or paid proxy service.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from curl_cffi.requests import AsyncSession

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from matcher import (
    Candidate,
    MatchResult,
    SearchTerms,
    build_search_terms,
    condition_tokens,
    dls_to_matcher_grade,
    pick_best_match,
    pick_lowest_price,
    score_candidate,
)
import supabase_io


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

# Base host. Mirror `m.bestbuy.ca` returns identical payloads if this is ever
# degraded; we don't currently fail over automatically.
_API_BASE = "https://www.bestbuy.ca"

# Single canonical User-Agent. Don't rotate.
_USER_AGENT = "BestBuyCanada/12.5.0 (Android 14; SM-S918W) okhttp/4.12.0"

# curl_cffi impersonation profile — gives us real Chrome's TLS + HTTP/2
# fingerprint so the request doesn't look like raw Python.
_IMPERSONATE = "chrome124"

# Canary SKU used at run start to verify the API is reachable.
_CANARY_SKU = "16709017"

# Concurrency + timing
MAX_CONCURRENCY_DEFAULT = 10
REQUEST_TIMEOUT = 12        # seconds
SEARCH_RETRIES = 3
RETRY_BACKOFFS = [1.0, 2.0, 4.0]
POLITE_DELAY_MIN = 0.5
POLITE_DELAY_MAX = 1.5


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging() -> logging.Logger:
    Path("logs").mkdir(exist_ok=True)
    Path("results").mkdir(exist_ok=True)

    log_path = Path("logs") / f"scraper_{datetime.now().strftime('%Y-%m-%d')}.log"
    error_path = Path("logs") / "errors.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    eh = logging.FileHandler(error_path, encoding="utf-8")
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    root.addHandler(eh)

    return logging.getLogger("bestbuy")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def make_session() -> AsyncSession:
    """Return a curl_cffi AsyncSession configured for the BB JSON endpoints."""
    return AsyncSession(
        impersonate=_IMPERSONATE,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-CA,en;q=0.9",
        },
        timeout=REQUEST_TIMEOUT,
    )


def extract_sku_from_url(product_url: Optional[str]) -> Optional[str]:
    """
    Extract the trailing numeric SKU from a stored bestbuy_listings.product_url.
    BB product URLs look like:
        https://www.bestbuy.ca/en-ca/product/<slug>/<sku>
    where <sku> is 6-8 digits. Returns None if pattern doesn't match.
    """
    if not product_url:
        return None
    m = re.search(r"/(\d{6,8})(?:\?|#|/|$)", product_url)
    return m.group(1) if m else None


async def get_product_by_sku(sku: str, session: AsyncSession) -> Optional[dict]:
    """
    Fetch product detail JSON by SKU. Returns parsed dict on 200, None on 404.
    Raises after retries on other non-200 statuses or network errors.

    Endpoint kept in code only per ops policy; not logged or documented elsewhere.
    """
    logger = logging.getLogger("bestbuy.http")
    url = f"{_API_BASE}/api/v2/json/product/{sku}"
    last_err: Optional[Exception] = None
    for attempt in range(SEARCH_RETRIES):
        try:
            resp = await session.get(url)
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                return resp.json()
            logger.warning("product %s -> status=%d (attempt %d/%d)",
                           sku, resp.status_code, attempt + 1, SEARCH_RETRIES)
            last_err = RuntimeError(f"product_fetch_status_{resp.status_code}")
        except Exception as exc:
            last_err = exc
            logger.warning("product %s network error (attempt %d/%d): %s",
                           sku, attempt + 1, SEARCH_RETRIES, str(exc)[:120])
        if attempt < SEARCH_RETRIES - 1:
            await asyncio.sleep(RETRY_BACKOFFS[attempt])
    raise RuntimeError(f"get_product_by_sku failed for {sku}: {last_err}")


async def search_products(
    query: str,
    session: AsyncSession,
    page: int = 1,
    page_size: int = 24,
) -> Tuple[List[Candidate], Dict[str, dict]]:
    """
    Search the BB catalog by keyword. Returns (candidates, raw_by_url) where:
      - candidates: List[Candidate(name, price, url)] for matcher.pick_best_match
      - raw_by_url: Dict[absolute_product_url -> full product dict from response]
        so the caller can attach the original record to the matched winner.

    Only `query=` is honoured by the upstream API as a real filter;
    `q=` and `keywords=` silently return the entire catalog. Do not change.

    Returns ([], {}) on empty result set. Raises after retries on non-200.
    """
    logger = logging.getLogger("bestbuy.http")
    qs = f"query={quote_plus(query)}&page={page}&pageSize={page_size}"
    url = f"{_API_BASE}/api/v2/json/search?{qs}"
    last_err: Optional[Exception] = None
    for attempt in range(SEARCH_RETRIES):
        try:
            resp = await session.get(url)
            if resp.status_code == 200:
                data = resp.json()
                products = data.get("products") or []
                candidates: List[Candidate] = []
                raw_by_url: Dict[str, dict] = {}
                for p in products:
                    name = (p.get("name") or "").strip()
                    rel = p.get("productUrl") or ""
                    if not name or not rel:
                        continue
                    abs_url = rel if rel.startswith("http") else f"{_API_BASE}{rel}"
                    price = _pick_price(p)
                    candidates.append(Candidate(name=name, price=price, url=abs_url))
                    raw_by_url[abs_url] = p
                return candidates, raw_by_url
            logger.warning("search '%s' p=%d -> status=%d (attempt %d/%d)",
                           query, page, resp.status_code, attempt + 1, SEARCH_RETRIES)
            last_err = RuntimeError(f"search_failed_{resp.status_code}")
        except Exception as exc:
            last_err = exc
            logger.warning("search '%s' p=%d network error (attempt %d/%d): %s",
                           query, page, attempt + 1, SEARCH_RETRIES, str(exc)[:120])
        if attempt < SEARCH_RETRIES - 1:
            await asyncio.sleep(RETRY_BACKOFFS[attempt])
    raise RuntimeError(f"search_products failed for '{query}': {last_err}")


def _pick_price(prod: dict) -> Optional[float]:
    """salePrice when on sale, regularPrice otherwise. None if neither."""
    sale = prod.get("salePrice")
    on_sale = prod.get("isProductOnSale") or prod.get("hasPromotion")
    if on_sale and isinstance(sale, (int, float)) and sale > 0:
        return float(sale)
    if isinstance(sale, (int, float)) and sale > 0:
        return float(sale)
    reg = prod.get("regularPrice")
    if isinstance(reg, (int, float)) and reg > 0:
        return float(reg)
    return None


async def api_canary(session: AsyncSession) -> bool:
    """Smoke-test the product endpoint. True iff it returns the expected SKU."""
    logger = logging.getLogger("bestbuy.canary")
    try:
        data = await get_product_by_sku(_CANARY_SKU, session)
    except Exception as exc:
        logger.error("Canary HTTP failed: %s", exc)
        return False
    if not data:
        logger.error("Canary returned no data (got None / 404).")
        return False
    if data.get("sku") != _CANARY_SKU:
        logger.error("Canary response SKU mismatch: expected %s, got %s",
                     _CANARY_SKU, data.get("sku"))
        return False
    logger.info("Canary OK — API endpoint reachable (sku=%s, name=%r)",
                data.get("sku"), (data.get("name") or "")[:60])
    return True


async def polite_delay() -> None:
    await asyncio.sleep(random.uniform(POLITE_DELAY_MIN, POLITE_DELAY_MAX))


# --------------------------------------------------------------------------- #
# Build competing_offers JSONB payload
# --------------------------------------------------------------------------- #


def _build_competing_offers(prod: dict, api_source: str) -> Dict[str, Any]:
    """
    Build the competing_offers JSONB payload from a product dict.

    Backward-compat note: the Dashboard reads keys main_price, marketplace_prices,
    and matched_name from this blob — those keys MUST remain present. We add new
    keys alongside; we do not rename existing ones.

    CAPABILITY REDUCTION: under the previous Playwright pipeline, marketplace_prices
    sometimes held multiple float values when a product page exposed competing
    third-party sellers in its "More sellers" section. The JSON API returns a
    SINGLE product record per request (whichever seller's offer matched the
    search), so marketplace_prices is now always []. Listings that previously
    captured multiple competing offers will now only have the matched-seller
    price. This is intentional, not a bug.
    """
    name = prod.get("name")
    sale = prod.get("salePrice")
    reg = prod.get("regularPrice")
    on_sale = bool(prod.get("isProductOnSale"))
    main = _pick_price(prod)

    seller_obj = prod.get("seller") or {}
    if not isinstance(seller_obj, dict):
        seller_obj = {}

    return {
        # === existing Dashboard-facing keys (do not rename) ===
        "main_price": main,
        "marketplace_prices": [],
        "matched_name": name,

        # === new keys from the JSON API ===
        "sku": prod.get("sku"),
        "is_marketplace": bool(prod.get("isMarketplace")),
        "is_available": bool(
            prod.get("isAvailableOnline") or prod.get("isPurchasable")
        ),
        "is_purchasable": bool(prod.get("isPurchasable")),
        "is_online_only": bool(prod.get("isOnlineOnly")),
        "is_clearance": bool(prod.get("isClearance")),
        "regular_price": reg if isinstance(reg, (int, float)) else None,
        "sale_price": sale if isinstance(sale, (int, float)) else None,
        "is_on_sale": on_sale,
        "seller_id": seller_obj.get("id") or prod.get("sellerId"),
        "model_number": prod.get("modelNumber"),
        "brand_name": prod.get("brandName"),
        "api_source": api_source,  # "search" | "product"
    }


def _resolve_seller_name(prod: dict) -> str:
    """Best-effort seller name string for buy_box_seller column."""
    s = prod.get("seller")
    if isinstance(s, dict) and s.get("name"):
        return str(s["name"])
    return "Best Buy"


def _absolutize_url(prod_url: Optional[str]) -> Optional[str]:
    """Prepend the API base if the URL is relative (search responses are)."""
    if not prod_url:
        return None
    if prod_url.startswith("http"):
        return prod_url
    if prod_url.startswith("/"):
        return f"{_API_BASE}{prod_url}"
    return prod_url


# --------------------------------------------------------------------------- #
# Row processing
# --------------------------------------------------------------------------- #


class RunStats:
    def __init__(self) -> None:
        self.attempted = 0
        self.matched = 0
        self.unmatched = 0
        self.errors = 0
        self.lock = asyncio.Lock()

    async def bump(self, field: str) -> None:
        async with self.lock:
            setattr(self, field, getattr(self, field) + 1)


async def _try_cached_path(
    cached_url: str,
    terms: SearchTerms,
    matcher_grade: str,
    session: AsyncSession,
) -> Optional[Tuple[dict, str]]:
    """
    Cache-first path: extract SKU, fetch by SKU, verify name passes the
    matcher's grade hard-fail. Returns (raw_product_dict, absolute_url) on
    valid hit, None on cache miss / verify failure.
    """
    logger = logging.getLogger("bestbuy.cache")
    sku = extract_sku_from_url(cached_url)
    if not sku:
        logger.info("Cache miss (sku not parseable from %s)", cached_url)
        return None
    try:
        prod = await get_product_by_sku(sku, session)
    except Exception as exc:
        logger.warning("Cache fetch errored for sku=%s: %s", sku, exc)
        return None
    if prod is None:
        logger.info("Cache miss (404 for sku=%s)", sku)
        return None

    name = prod.get("name") or ""
    price = _pick_price(prod)
    abs_url = _absolutize_url(prod.get("productUrl")) or cached_url

    cand = Candidate(name=name, price=price, url=abs_url)
    score = score_candidate(cand, terms, grade=matcher_grade)
    if score < 5:
        logger.info(
            "Cache miss (score %d < 5) sku=%s name=%r — silent condition-tier drift?",
            score, sku, name[:80],
        )
        return None
    logger.info("Cache hit (score %d) sku=%s name=%r", score, sku, name[:80])
    return prod, abs_url


async def _search_path(
    terms: SearchTerms,
    matcher_grade: str,
    session: AsyncSession,
) -> Tuple[Optional[MatchResult], Optional[dict], Optional[str]]:
    """
    Search path with the same query-fallback chain the old scraper used:
    primary query → without color → without storage → watch-unlocked.
    Returns (match_result, raw_winner_product, absolute_winner_url).
    """
    logger = logging.getLogger("bestbuy.search")

    queries = [terms.query()]
    if terms.color:
        queries.append(terms.query_without_color())
    if terms.storage and not terms.is_watch:
        queries.append(terms.query_without_storage())
    if terms.is_watch:
        queries.append(terms.query_watch_unlocked())

    seen_q = set()
    for q in queries:
        if not q or q in seen_q:
            continue
        seen_q.add(q)
        logger.info("Searching: %s", q)
        candidates, raw_by_url = await search_products(q, session)
        if not candidates:
            continue
        match = pick_best_match(candidates, terms, grade=matcher_grade)
        if match.candidate:
            winner = raw_by_url.get(match.candidate.url)
            return match, winner, match.candidate.url

    return None, None, None


async def process_row(
    row: Dict[str, Any],
    idx: int,
    total: int,
    client,
    csv_writer: csv.DictWriter,
    csv_file,
    semaphore: asyncio.Semaphore,
    stats: RunStats,
    dry_run: bool,
    session: AsyncSession,
) -> None:
    logger = logging.getLogger("bestbuy.row")
    listing_id = row["listing_id"]
    terms = build_search_terms(row)
    matcher_grade = dls_to_matcher_grade(row.get("grade"))
    cached_url = row.get("existing_bb_url")
    query = terms.query()

    logger.info(
        "[%d/%d] listing %s: %s %s | dls=%s -> matcher=%s | cache=%s",
        idx, total, listing_id, row.get("make"), row.get("model"),
        row.get("grade"), matcher_grade or "(none)",
        "yes" if cached_url else "no",
    )

    if not terms.brand and not terms.model:
        logger.info("[%d/%d] listing %s: blank make/model — skipping",
                    idx, total, listing_id)
        return

    if dry_run:
        logger.info(
            "[DRY RUN] listing %s: would search %r (cached_url=%s)",
            listing_id, query, cached_url,
        )
        await stats.bump("attempted")
        return

    await stats.bump("attempted")

    async with semaphore:
        prod: Optional[dict] = None
        winner_url: Optional[str] = None
        is_approx = False
        api_source = "search"

        try:
            # === cache-first ===
            if cached_url:
                cached_hit = await _try_cached_path(
                    cached_url, terms, matcher_grade, session,
                )
                if cached_hit:
                    prod, winner_url = cached_hit
                    api_source = "product"
                    is_approx = False  # cache verified, treat as exact

            # === search fallback ===
            if prod is None:
                match, winner, w_url = await _search_path(
                    terms, matcher_grade, session,
                )
                if not match or not match.candidate or winner is None:
                    logger.info("[%d/%d] listing %s: no match found",
                                idx, total, listing_id)
                    supabase_io.write_scrape_failure(
                        client, listing_id,
                        reason="no_match",
                        url_attempted=cached_url,
                        query=query,
                    )
                    _write_csv_row(csv_writer, csv_file, listing_id, terms,
                                   None, "", "Not Found")
                    await stats.bump("unmatched")
                    return
                prod = winner
                winner_url = w_url
                is_approx = match.is_approximate
                api_source = "search"

        except Exception as exc:
            logger.exception("[%d/%d] listing %s: error: %s",
                             idx, total, listing_id, exc)
            try:
                supabase_io.write_scrape_failure(
                    client, listing_id,
                    reason=f"error: {exc}"[:500],
                    url_attempted=cached_url,
                    query=query,
                )
            except Exception:
                pass
            _write_csv_row(csv_writer, csv_file, listing_id, terms,
                           None, "", "Error")
            await stats.bump("errors")
            return

        # === resolve price + write ===
        price = _pick_price(prod)
        if price is None or price <= 0:
            logger.info(
                "[%d/%d] listing %s: matched but no price (sku=%s)",
                idx, total, listing_id, prod.get("sku"),
            )
            supabase_io.write_scrape_failure(
                client, listing_id,
                reason="no_price_on_page",
                url_attempted=winner_url,
                query=query,
            )
            _write_csv_row(csv_writer, csv_file, listing_id, terms,
                           None, winner_url or "", "No price")
            await stats.bump("unmatched")
            return

        winner_url = winner_url or _absolutize_url(prod.get("productUrl")) or ""
        match_name = prod.get("name") or ""
        seller_name = _resolve_seller_name(prod)
        competing_offers = _build_competing_offers(prod, api_source=api_source)

        try:
            supabase_io.write_scrape_success(
                client=client,
                listing_id=listing_id,
                url=winner_url,
                matched_bb_name=match_name,
                main_price=price,
                marketplace_prices=[],   # see _build_competing_offers comment
                lowest_price=price,
                is_approximate=is_approx,
                buy_box_seller=seller_name,
                assurant_make=row.get("make"),
                assurant_model=row.get("model"),
                assurant_capacity=row.get("capacity"),
                assurant_color=row.get("color"),
                competing_offers_extras=competing_offers,
            )
        except Exception as exc:
            logger.exception("[%d/%d] listing %s: write failed: %s",
                             idx, total, listing_id, exc)
            await stats.bump("errors")
            _write_csv_row(csv_writer, csv_file, listing_id, terms,
                           price, winner_url, "Write failed")
            return

        note = "(Approx Match)" if is_approx else "Matched"
        logger.info(
            "[%d/%d] listing %s: $%.2f CAD %s %s [%s]",
            idx, total, listing_id, price, note, winner_url, api_source,
        )
        _write_csv_row(csv_writer, csv_file, listing_id, terms,
                       price, winner_url, note)
        await stats.bump("matched")

    await polite_delay()


# --------------------------------------------------------------------------- #
# CSV backup
# --------------------------------------------------------------------------- #


def _open_csv_backup():
    path = Path("results") / f"prices_{datetime.now().strftime('%Y-%m-%d')}.csv"
    new_file = not path.exists()
    f = open(path, "a", newline="", encoding="utf-8")
    fieldnames = [
        "listing_id", "brand", "model", "storage", "color",
        "query", "price_cad", "url", "status", "timestamp",
    ]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    if new_file:
        writer.writeheader()
    return writer, f, path


def _write_csv_row(
    writer: csv.DictWriter,
    file,
    listing_id: int,
    terms: SearchTerms,
    price: Optional[float],
    url: str,
    status: str,
) -> None:
    writer.writerow({
        "listing_id": listing_id,
        "brand": terms.brand,
        "model": terms.model,
        "storage": terms.storage or "",
        "color": terms.color or "",
        "query": terms.query(),
        "price_cad": f"{price:.2f}" if price is not None else "",
        "url": url,
        "status": status,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    file.flush()


# --------------------------------------------------------------------------- #
# Run summary
# --------------------------------------------------------------------------- #


def emit_run_summary(
    stats: RunStats,
    duration_seconds: float,
    mode: str = "unmatched_only",
) -> None:
    logger = logging.getLogger("bestbuy")
    attempted = stats.attempted
    matched = stats.matched
    unmatched = stats.unmatched
    errors = stats.errors

    line = (
        f"RUN SUMMARY mode={mode} attempted={attempted} matched={matched} "
        f"unmatched={unmatched} errors={errors} "
        f"duration_seconds={duration_seconds:.1f}"
    )
    logger.info(line)
    print(line, flush=True)

    match_rate = (matched / attempted) if attempted else 0.0
    level = "warning"
    if mode == "single" or attempted == 0 or match_rate >= 0.8:
        level = "notice"
    print(
        f"::{level}::BestBuy scrape {line} match_rate={match_rate:.2%}",
        flush=True,
    )


# --------------------------------------------------------------------------- #
# CLI + main
# --------------------------------------------------------------------------- #


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BestBuy Canada price scraper")
    p.add_argument("--dry-run", action="store_true",
                   help="Log what would be scraped, but don't hit BestBuy or write to Supabase")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N matching rows (batch mode only)")
    p.add_argument("--force", action="store_true",
                   help="Operator override: include matched listings as well "
                        "as unmatched ones. TTL filter still applies.")
    p.add_argument("--unmatched-only", action="store_true",
                   help="Explicit alias for default cron behavior — only "
                        "process listings without a cached BB match. No-op "
                        "when neither --force nor --listing-id is set.")
    p.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY_DEFAULT,
                   help=f"Max concurrent HTTP requests (default {MAX_CONCURRENCY_DEFAULT})")
    p.add_argument("--listing-id", type=int, default=None,
                   help="Single-listing dispatch mode. Wins over --force / --limit.")
    return p.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    logger = setup_logging()
    ttl_hours = int(os.environ.get("BESTBUY_PRICE_TTL_HOURS", "24"))

    single_listing_id: Optional[int] = args.listing_id
    if single_listing_id is not None:
        mode = "single"
        ignored = []
        if args.force:
            ignored.append("--force")
        if args.limit is not None:
            ignored.append("--limit")
        if args.unmatched_only:
            ignored.append("--unmatched-only")
        if ignored:
            logger.warning(
                "Single-listing mode (--listing-id=%d) overrides %s",
                single_listing_id, ", ".join(ignored),
            )
    elif args.force:
        mode = "force"
        if args.unmatched_only:
            logger.warning("--force overrides --unmatched-only")
    else:
        mode = "unmatched_only"

    logger.info(
        "BestBuy scraper starting | mode=%s concurrency=%d "
        "dry_run=%s force=%s unmatched_only=%s limit=%s listing_id=%s ttl_hours=%d",
        mode, args.concurrency, args.dry_run, args.force, args.unmatched_only,
        args.limit, single_listing_id, ttl_hours,
    )

    try:
        client = supabase_io.get_client()
    except Exception as exc:
        logger.error("Supabase client init failed: %s", exc)
        return 2

    # === API canary ===
    async with make_session() as canary_session:
        ok = await api_canary(canary_session)
    if not ok:
        logger.error("API canary failed — aborting before fetching cohort.")
        return 2

    # === fetch cohort ===
    try:
        if mode == "single":
            logger.info("Single-listing mode: processing assurant_listing_id=%d",
                        single_listing_id)
            row = supabase_io.read_single_listing(client, single_listing_id)
            if row is None:
                logger.error("Listing %d not found", single_listing_id)
                emit_run_summary(RunStats(), 0.0, mode=mode)
                return 4
            rows = [row]
        else:
            rows = supabase_io.read_biddable_listings_needing_bb_prices(
                client, ttl_hours=ttl_hours, force=args.force, limit=args.limit,
            )
    except Exception as exc:
        logger.exception("Read from Supabase failed: %s", exc)
        return 3

    total = len(rows)
    if total == 0:
        logger.warning("No listings need scraping right now")
        emit_run_summary(RunStats(), 0.0, mode=mode)
        return 0

    csv_writer, csv_file, csv_path = _open_csv_backup()
    logger.info("CSV backup: %s", csv_path)

    semaphore = asyncio.Semaphore(args.concurrency)
    stats = RunStats()
    started = time.monotonic()

    async with make_session() as session:
        tasks = [
            process_row(
                row=row, idx=idx, total=total,
                client=client, csv_writer=csv_writer, csv_file=csv_file,
                semaphore=semaphore, stats=stats, dry_run=args.dry_run,
                session=session,
            )
            for idx, row in enumerate(rows, start=1)
        ]
        await asyncio.gather(*tasks, return_exceptions=False)

    csv_file.close()
    duration = time.monotonic() - started
    emit_run_summary(stats, duration, mode=mode)
    return 0


def main() -> None:
    args = parse_args()
    try:
        code = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logging.getLogger("bestbuy").warning("Interrupted by user")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
