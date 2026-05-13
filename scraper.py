"""
scraper.py — BestBuy Canada price scraper.

Reads in-stock phone listings (with accepted grades) from Supabase, looks
each one up on bestbuy.ca, extracts the BestBuy main + marketplace seller
prices, and writes the lowest as a new bestbuy_prices row (with upserted
bestbuy_listings + assurant_listing_matches).

Workflow per row:
  1. If a cached BestBuy URL exists in assurant_listing_matches and is still
     valid, fetch its product page directly (cache-first).
  2. Otherwise (or on cache miss), run the search + fallback query chain and
     pick the best match using matcher.py.
  3. Extract main + marketplace seller prices from the product page.
  4. Write lowest price to bestbuy_prices, upsert listing + match rows.
  5. Append to results/prices_YYYY-MM-DD.csv as a CI artifact backup.

Anti-detection (carried over from the source scraper, unchanged):
  - Fresh Chromium per row, UA Chrome/131 Windows, viewport 1920x1080
  - en-CA locale, America/Toronto timezone
  - navigator.webdriver patched to undefined
  - 2-4s polite delay between requests, 3 retries with exponential backoff
  - Concurrency via asyncio.Semaphore (default 5)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
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

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv optional in CI
    pass

from matcher import (
    Candidate,
    MatchResult,
    SearchTerms,
    build_search_terms,
    dls_to_matcher_grade,
    pick_best_match,
    pick_lowest_price,
    score_candidate,
)
import supabase_io


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BESTBUY_BASE = "https://www.bestbuy.ca"
SEARCH_URL_TEMPLATE = "https://www.bestbuy.ca/en-ca/search?search={query}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1920, "height": 1080}

MAX_CONCURRENCY_DEFAULT = 5
MAX_RETRIES = 3
REQUEST_DELAY_MIN = 2.0
REQUEST_DELAY_MAX = 4.0
NAV_TIMEOUT_MS = 45_000
SELECTOR_TIMEOUT_MS = 15_000


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
# Playwright helpers
# --------------------------------------------------------------------------- #


async def new_context(playwright: Playwright) -> Tuple[BrowserContext, object]:
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
        ],
    )
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport=VIEWPORT,
        locale="en-CA",
        timezone_id="America/Toronto",
        extra_http_headers={
            "Accept-Language": "en-CA,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return context, browser


async def polite_delay() -> None:
    await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


async def _close(context, browser) -> None:
    try:
        if context:
            await context.close()
    except Exception:
        pass
    try:
        if browser:
            await browser.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# BestBuy page scraping
# --------------------------------------------------------------------------- #


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


async def search_bestbuy(page: Page, query: str) -> List[Candidate]:
    url = SEARCH_URL_TEMPLATE.format(query=quote_plus(query))
    logger = logging.getLogger("bestbuy.search")
    logger.info("Searching: %s", query)
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    try:
        await page.wait_for_selector(
            "li.x-productListItem, h3[class*='productItemName'], "
            "div[class*='productList'], p[class*='noResults'], "
            "div[class*='noSearchResult']",
            timeout=SELECTOR_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.warning("Search page selector timeout for: %s", query)
        return []

    try:
        close_btn = page.locator(
            "button:has-text('Close'), button[aria-label='Close']"
        ).first
        if await close_btn.is_visible(timeout=1000):
            await close_btn.click()
    except Exception:
        pass

    await page.evaluate("window.scrollBy(0, 800)")
    await asyncio.sleep(1.5)

    raw: List[Dict[str, str]] = await page.evaluate(
        """
        () => {
            const out = [];
            const items = document.querySelectorAll(
                "li.x-productListItem, li[class*='productLine']"
            );
            for (const item of items) {
                const link = item.querySelector("a[href*='/product/']");
                if (!link) continue;
                const href = link.getAttribute('href') || '';
                const nameEl = item.querySelector(
                    "h3[class*='productItemName'], div[class*='productItemName'], " +
                    "[data-automation='productItemName']"
                );
                const name = nameEl ? nameEl.textContent.trim() : link.textContent.trim();
                let priceText = '';
                const priceEl = item.querySelector(
                    "div[class*='price__'], span[class*='price__'], [class*='productItemPrice']"
                );
                if (priceEl) priceText = priceEl.textContent.trim();
                if (!priceText) {
                    const all = item.textContent || '';
                    const m = all.match(/\\$[\\d,]+\\.\\d{2}/);
                    if (m) priceText = m[0];
                }
                if (name && name.length > 3) {
                    out.push({ name, href, priceText });
                }
            }
            return out;
        }
        """
    )

    out: List[Candidate] = []
    for c in raw:
        name = (c.get("name") or "").strip()
        href = (c.get("href") or "").strip()
        if not name or not href:
            continue
        url_abs = href if href.startswith("http") else f"{BESTBUY_BASE}{href}"
        out.append(Candidate(name=name, price=_parse_price(c.get("priceText") or ""), url=url_abs))

    logger.info("Found %d candidates for: %s", len(out), query)
    return out


async def fetch_product_page(
    page: Page, product_url: str
) -> Tuple[Optional[str], Optional[float], List[float]]:
    """
    Open a product page. Returns (product_name, main_price, marketplace_prices).
    Returns (None, None, []) on navigation/parse failure.
    """
    logger = logging.getLogger("bestbuy.product")
    try:
        await page.goto(product_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        logger.warning("Product page navigation timeout: %s", product_url)
        return None, None, []

    try:
        await page.wait_for_selector(
            "div[class*='price__'], span[class*='price__'], [class*='productPricing']",
            timeout=SELECTOR_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        logger.warning("Product page price selector timeout: %s", product_url)
        return None, None, []

    data = await page.evaluate(
        """
        () => {
            const r = { name: null, main: null, marketplace: [] };
            const titleEl = document.querySelector(
                "h1[class*='productName'], h1[data-automation='product-title'], h1"
            );
            if (titleEl) r.name = (titleEl.textContent || '').trim();
            const priceEl = document.querySelector(
                "div[class*='price__'], span[class*='price__'], " +
                "[class*='productPricing'] [class*='price']"
            );
            if (priceEl) {
                const t = priceEl.textContent || '';
                const m = t.match(/\\$[\\d,]+\\.\\d{2}/);
                r.main = m ? m[0] : t.trim();
            }
            const sections = document.querySelectorAll(
                "div[class*='marketplaceSeller'], div[class*='otherSeller'], " +
                "section[class*='marketplace'], div[class*='moreSeller']"
            );
            for (const s of sections) {
                for (const p of s.querySelectorAll("[class*='price']")) {
                    const t = (p.textContent || '').trim();
                    if (t) r.marketplace.push(t);
                }
            }
            return r;
        }
        """
    )

    name = data.get("name")
    main_price = _parse_price(data.get("main") or "")
    marketplace = [p for p in (_parse_price(t) for t in data.get("marketplace", [])) if p]
    return name, main_price, marketplace


# --------------------------------------------------------------------------- #
# Scrape one listing
# --------------------------------------------------------------------------- #


async def _try_cached_url(
    playwright: Playwright,
    cached_url: str,
    terms: SearchTerms,
    matcher_grade: str,
) -> Optional[Tuple[str, str, Optional[float], List[float]]]:
    """
    Try the cached URL directly. Returns (name, url, main_price, marketplace)
    if the cached page looks like a valid match for this listing; else None.
    """
    logger = logging.getLogger("bestbuy.cache")
    context = browser = None
    try:
        context, browser = await new_context(playwright)
        page = await context.new_page()
        name, main_price, marketplace = await fetch_product_page(page, cached_url)
        if not name:
            logger.info("Cache miss (no name extracted): %s", cached_url)
            return None
        if main_price is None and not marketplace:
            logger.info("Cache miss (no price on page): %s", cached_url)
            return None
        # Confirm the cached product is still a sensible match.
        cand = Candidate(name=name, price=main_price, url=cached_url)
        score = score_candidate(cand, terms, grade=matcher_grade)
        if score < 5:
            logger.info(
                "Cache miss (score %d < 5) for %s — cached page no longer matches",
                score, cached_url,
            )
            return None
        logger.info("Cache hit (score %d): %s", score, cached_url)
        return name, cached_url, main_price, marketplace
    except Exception as exc:
        logger.warning("Cache attempt errored: %s", exc)
        return None
    finally:
        await _close(context, browser)


async def _search_and_match(
    playwright: Playwright,
    terms: SearchTerms,
    matcher_grade: str,
) -> Tuple[Optional[MatchResult], Optional[Tuple[Optional[float], List[float]]]]:
    """
    Full search + fallback chain + product-page fetch. Returns (match, prices)
    where prices = (main_price, marketplace_prices) for the matched URL, or
    (None, None) if nothing matched.
    """
    logger = logging.getLogger("bestbuy.search")
    last_err: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        context = browser = None
        try:
            context, browser = await new_context(playwright)
            page = await context.new_page()

            candidates = await search_bestbuy(page, terms.query())
            match = pick_best_match(candidates, terms, grade=matcher_grade)

            if not match.candidate and terms.color:
                logger.info("Retrying without color")
                candidates = await search_bestbuy(page, terms.query_without_color())
                match = pick_best_match(candidates, terms, grade=matcher_grade)

            if not match.candidate and terms.storage and not terms.is_watch:
                logger.info("Retrying without storage")
                candidates = await search_bestbuy(page, terms.query_without_storage())
                match = pick_best_match(candidates, terms, grade=matcher_grade)

            if not match.candidate and terms.is_watch:
                logger.info("Retrying watch: brand + model only")
                candidates = await search_bestbuy(page, terms.query_watch_unlocked())
                match = pick_best_match(candidates, terms, grade=matcher_grade)

            if not match.candidate:
                await _close(context, browser)
                return None, None

            _, main_price, marketplace = await fetch_product_page(page, match.candidate.url)
            await _close(context, browser)
            return match, (main_price, marketplace)

        except PlaywrightTimeoutError as exc:
            last_err = exc
            logger.warning("Timeout attempt %d/%d: %s", attempt, MAX_RETRIES, exc)
        except Exception as exc:
            last_err = exc
            logger.warning("Error attempt %d/%d: %s", attempt, MAX_RETRIES, exc)
        finally:
            await _close(context, browser)

        await asyncio.sleep(2 ** attempt + random.uniform(0, 1))

    logger.error("All %d search attempts failed: %s", MAX_RETRIES, last_err)
    raise RuntimeError(f"Search failed after {MAX_RETRIES} retries: {last_err}")


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


async def process_row(
    playwright: Optional[Playwright],
    row: Dict[str, Any],
    idx: int,
    total: int,
    client,  # supabase.Client (runtime type)
    csv_writer: csv.DictWriter,
    csv_file,
    semaphore: asyncio.Semaphore,
    stats: RunStats,
    dry_run: bool,
) -> None:
    logger = logging.getLogger("bestbuy.row")
    listing_id = row["listing_id"]
    terms = build_search_terms(row)
    matcher_grade = dls_to_matcher_grade(row.get("grade"))

    if not terms.brand and not terms.model:
        logger.info("[%d/%d] listing %s: blank make/model — skipping", idx, total, listing_id)
        return

    query = terms.query()
    cached_url = row.get("existing_bb_url")
    logger.info(
        "[%d/%d] listing %s: %s %s | dls=%s -> matcher=%s | cache=%s",
        idx, total, listing_id, row.get("make"), row.get("model"),
        row.get("grade"), matcher_grade or "(none)",
        "yes" if cached_url else "no",
    )

    if dry_run:
        logger.info(
            "[DRY RUN] listing %s: would search %r (cached_url=%s)",
            listing_id, query, cached_url,
        )
        await stats.bump("attempted")
        return

    await stats.bump("attempted")

    async with semaphore:
        try:
            match_name: Optional[str] = None
            match_url: Optional[str] = None
            is_approx = False
            main_price: Optional[float] = None
            marketplace: List[float] = []

            # Step 1: cache-first.
            cached_result = None
            if cached_url:
                cached_result = await _try_cached_url(
                    playwright, cached_url, terms, matcher_grade
                )

            if cached_result:
                match_name, match_url, main_price, marketplace = cached_result
                is_approx = False  # we trust the cache's prior match confidence
            else:
                # Step 2: search fallback.
                match, prices = await _search_and_match(playwright, terms, matcher_grade)
                if not match or not match.candidate:
                    logger.info("[%d/%d] listing %s: no match found", idx, total, listing_id)
                    supabase_io.write_scrape_failure(
                        client, listing_id,
                        reason="no_match",
                        url_attempted=cached_url,
                        query=query,
                    )
                    _write_csv_row(csv_writer, csv_file, listing_id, terms, None, "", "Not Found")
                    await stats.bump("unmatched")
                    return
                match_name = match.candidate.name
                match_url = match.candidate.url
                is_approx = match.is_approximate
                main_price, marketplace = prices or (None, [])

        except Exception as exc:
            logger.exception("[%d/%d] listing %s: error: %s", idx, total, listing_id, exc)
            try:
                supabase_io.write_scrape_failure(
                    client, listing_id,
                    reason=f"error: {exc}"[:500],
                    url_attempted=cached_url,
                    query=query,
                )
            except Exception:
                pass
            _write_csv_row(csv_writer, csv_file, listing_id, terms, None, "", "Error")
            await stats.bump("errors")
            return

        # Compute lowest.
        effective = Candidate(name=match_name or "", price=main_price, url=match_url or "")
        lowest = pick_lowest_price(effective, marketplace)
        if lowest <= 0:
            logger.info(
                "[%d/%d] listing %s: matched %s but no price on page",
                idx, total, listing_id, match_url,
            )
            supabase_io.write_scrape_failure(
                client, listing_id,
                reason="no_price_on_page",
                url_attempted=match_url,
                query=query,
            )
            _write_csv_row(csv_writer, csv_file, listing_id, terms, None, match_url or "", "No price")
            await stats.bump("unmatched")
            return

        try:
            supabase_io.write_scrape_success(
                client=client,
                listing_id=listing_id,
                url=match_url,
                matched_bb_name=match_name,
                main_price=main_price,
                marketplace_prices=marketplace,
                lowest_price=lowest,
                is_approximate=is_approx,
                assurant_make=row.get("make"),
                assurant_model=row.get("model"),
                assurant_capacity=row.get("capacity"),
                assurant_color=row.get("color"),
            )
        except Exception as exc:
            logger.exception("[%d/%d] listing %s: write failed: %s", idx, total, listing_id, exc)
            await stats.bump("errors")
            _write_csv_row(csv_writer, csv_file, listing_id, terms, lowest, match_url, "Write failed")
            return

        note = "(Approx Match)" if is_approx else "Matched"
        logger.info(
            "[%d/%d] listing %s: $%.2f CAD %s %s",
            idx, total, listing_id, lowest, note, match_url,
        )
        _write_csv_row(csv_writer, csv_file, listing_id, terms, lowest, match_url, note)
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
    # Single-listing failures are normal (operator may pick a hard one); don't
    # raise the GH Actions warning over a single-row mismatch. Batch modes do.
    level = "warning"
    if mode == "single" or attempted == 0 or match_rate >= 0.8:
        level = "notice"
    print(
        f"::{level}::BestBuy scrape {line} match_rate={match_rate:.2%}",
        flush=True,
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BestBuy Canada price scraper")
    p.add_argument("--dry-run", action="store_true",
                   help="Log what would be scraped, but don't hit BestBuy or write to Supabase")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N matching rows (batch mode only)")
    p.add_argument("--force", action="store_true",
                   help="Operator override: include matched listings too "
                        "(default cron path scrapes only unmatched listings). "
                        "TTL filter still applies to skip very recently scraped rows.")
    p.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY_DEFAULT,
                   help=f"Max concurrent browsers (default {MAX_CONCURRENCY_DEFAULT})")
    p.add_argument("--listing-id", type=int, default=None,
                   help="Single-listing dispatch mode: process exactly this "
                        "assurant_listings.listing_id, bypassing all eligibility "
                        "filtering. Used by the dashboard's per-listing Refresh "
                        "button. Wins over --force / --limit.")
    return p.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    logger = setup_logging()
    ttl_hours = int(os.environ.get("BESTBUY_PRICE_TTL_HOURS", "24"))

    # Mode resolution. Single-listing wins.
    single_listing_id: Optional[int] = args.listing_id
    if single_listing_id is not None:
        mode = "single"
        ignored = []
        if args.force:
            ignored.append("--force")
        if args.limit is not None:
            ignored.append("--limit")
        if ignored:
            logger.warning(
                "Single-listing mode (--listing-id=%d) overrides %s",
                single_listing_id, ", ".join(ignored),
            )
    elif args.force:
        mode = "force"
    else:
        mode = "unmatched_only"

    logger.info(
        "BestBuy scraper starting | mode=%s concurrency=%d "
        "dry_run=%s force=%s limit=%s listing_id=%s ttl_hours=%d",
        mode, args.concurrency, args.dry_run, args.force, args.limit,
        single_listing_id, ttl_hours,
    )

    try:
        client = supabase_io.get_client()
    except Exception as exc:
        logger.error("Supabase client init failed: %s", exc)
        return 2

    try:
        if mode == "single":
            logger.info(
                "Single-listing mode: processing assurant_listing_id=%d",
                single_listing_id,
            )
            row = supabase_io.read_single_listing(client, single_listing_id)
            if row is None:
                logger.error(
                    "Listing %d not found in assurant_listings", single_listing_id,
                )
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

    if args.dry_run:
        # No Playwright needed in dry-run; pass None.
        tasks = [
            process_row(
                playwright=None, row=row, idx=idx, total=total,
                client=client, csv_writer=csv_writer, csv_file=csv_file,
                semaphore=semaphore, stats=stats, dry_run=True,
            )
            for idx, row in enumerate(rows, start=1)
        ]
        await asyncio.gather(*tasks, return_exceptions=False)
    else:
        async with async_playwright() as playwright:
            tasks = [
                process_row(
                    playwright=playwright, row=row, idx=idx, total=total,
                    client=client, csv_writer=csv_writer, csv_file=csv_file,
                    semaphore=semaphore, stats=stats, dry_run=False,
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
