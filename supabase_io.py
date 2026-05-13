"""
supabase_io.py

Supabase read/write layer for the BestBuy price scraper. Uses the
service-role key so it can write to all tables. Never log the key.

Functions:
  - read_biddable_listings_needing_bb_prices(client, ttl_hours, force, limit)
      Default (force=False): returns ONLY listings with no existing BestBuy
      match (bestbuy_listing_id IS NULL). Matched listings — regardless of
      price age — are skipped; refresh them via the dashboard's per-listing
      Refresh button (which dispatches a single-listing run).

      force=True: returns all base candidates, with TTL-based filtering
      applied (skip listings whose most-recent bestbuy_prices.fetched_at is
      newer than ttl_hours).

  - read_single_listing(client, listing_id)
      Returns one assurant_listings row by listing_id (with embedded match /
      cached BB URL) regardless of stock, grade, or match state. Used by the
      single-listing dispatch path.

  - write_scrape_success(...)
  - write_scrape_failure(...)

Schema mapping (matches the upstream Supabase migrations):
  bestbuy_listings(product_url UNIQUE, product_title, bb_grade,
                   make, model, capacity, color, last_verified_at)
  bestbuy_prices  (bestbuy_listing_id FK, buy_box_price_cad, buy_box_seller,
                   competing_offers JSONB, fetched_at)
  assurant_listing_matches(assurant_listing_id PK, bestbuy_listing_id,
                           confidence, manually_verified, match_notes,
                           matched_at, last_rematch_at)
  agent_decisions(assurant_listing_id, decision, reasoning, inputs_json,
                  created_at)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from supabase import Client, create_client


logger = logging.getLogger(__name__)


ACCEPTED_GRADES = ("DLS A+", "DLS A-", "DLS B+", "DLS B-")
TOLERANT_CATEGORY_VALUES = ("phone", "phones", "cell phone", "smartphone")
TOLERANT_PHONE_MAKES = ("Apple", "Samsung", "Google", "Motorola", "OnePlus", "Nothing")

# PostgREST select expression — assurant_listings + embedded match + cached BB URL.
_LISTING_SELECT_EXPR = (
    "listing_id,item_id,warehouse,make,model,model_number,capacity,color,"
    "grade,currency,list_price,available_quantity,category,"
    "assurant_listing_matches("
    "  bestbuy_listing_id,"
    "  bestbuy_listings(id,product_url)"
    ")"
)


def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in the "
            "environment (see .env.example)."
        )
    return create_client(url, key)


# --------------------------------------------------------------------------- #
# Pure helpers — testable without a Supabase client
# --------------------------------------------------------------------------- #


def _embedded_match(raw_row: Dict[str, Any]) -> Dict[str, Any]:
    """
    PostgREST returns embedded related rows as either a list or a dict
    depending on cardinality / how it infers the relationship. Coerce to dict.
    """
    m = raw_row.get("assurant_listing_matches") or {}
    if isinstance(m, list):
        m = m[0] if m else {}
    return m or {}


def _embedded_bestbuy_listing(match: Dict[str, Any]) -> Dict[str, Any]:
    bb = match.get("bestbuy_listings") or {}
    if isinstance(bb, list):
        bb = bb[0] if bb else {}
    return bb or {}


def _flatten_row(raw_row: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an assurant_listings row + embedded match into the dict shape
    expected by scraper.process_row."""
    match = _embedded_match(raw_row)
    bb_listing_id = match.get("bestbuy_listing_id") if match else None
    bb_listing = _embedded_bestbuy_listing(match) if match else {}
    existing_bb_url = bb_listing.get("product_url") if bb_listing else None
    return {
        "listing_id": raw_row["listing_id"],
        "item_id": raw_row.get("item_id"),
        "warehouse": raw_row.get("warehouse"),
        "make": raw_row.get("make"),
        "model": raw_row.get("model"),
        "model_number": raw_row.get("model_number"),
        "capacity": raw_row.get("capacity"),
        "color": raw_row.get("color"),
        "grade": raw_row.get("grade"),
        "category": raw_row.get("category"),
        "currency": raw_row.get("currency"),
        "list_price": raw_row.get("list_price"),
        "available_quantity": raw_row.get("available_quantity"),
        "existing_bb_url": existing_bb_url,
        "existing_bb_listing_id": bb_listing_id,
    }


def _filter_eligible_listings(
    raw_rows: List[Dict[str, Any]],
    force: bool,
    fresh_bb_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """
    Apply the eligibility filter and flatten.

    Default mode (force=False):
        Exclude any row whose embedded assurant_listing_matches row has a
        non-NULL bestbuy_listing_id. (Matched listings are refreshed via the
        per-listing dashboard button, not this batch path.)

    Force mode (force=True):
        Include all base candidates EXCEPT those whose cached BB listing has
        a TTL-fresh price already (set via fresh_bb_ids).
    """
    fresh_bb_ids = fresh_bb_ids or set()
    out: List[Dict[str, Any]] = []
    for r in raw_rows:
        match = _embedded_match(r)
        bb_listing_id = match.get("bestbuy_listing_id") if match else None

        if not force:
            # Default: only-unmatched.
            if bb_listing_id is not None:
                continue
        else:
            # Force: TTL filter, but match presence is no longer a gate.
            if bb_listing_id is not None and bb_listing_id in fresh_bb_ids:
                continue

        out.append(_flatten_row(r))
    return out


# --------------------------------------------------------------------------- #
# Read — batch
# --------------------------------------------------------------------------- #


def read_biddable_listings_needing_bb_prices(
    client: Client,
    ttl_hours: int = 24,
    force: bool = False,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch in-stock biddable phone listings that need a BestBuy price.

    Base filter (always applied):
      available_quantity > 0
      AND grade IN ('DLS A+','DLS A-','DLS B+','DLS B-')
      AND ( LOWER(category) IN ('phone','phones','cell phone','smartphone')
            OR make IN (Apple, Samsung, Google, Motorola, OnePlus, Nothing) )

    Eligibility (on top of base filter):
      Default (force=False, the cron path):
        Only listings with NO existing BB match. Matched listings are
        skipped regardless of price age; operators refresh them via the
        dashboard.
      force=True (operator override):
        Include matched listings too, but skip those whose most-recent
        bestbuy_prices.fetched_at is newer than ttl_hours.
    """
    # Build the tolerant category/make OR filter for PostgREST.
    cat_clauses = [f"category.ilike.{v}" for v in TOLERANT_CATEGORY_VALUES]
    make_clauses = [f"make.in.({','.join(TOLERANT_PHONE_MAKES)})"]
    or_expr = ",".join(cat_clauses + make_clauses)

    resp = (
        client.table("assurant_listings")
        .select(_LISTING_SELECT_EXPR)
        .gt("available_quantity", 0)
        .in_("grade", list(ACCEPTED_GRADES))
        .or_(or_expr)
        .execute()
    )
    raw_rows: List[Dict[str, Any]] = resp.data or []
    logger.info("Fetched %d base candidate rows from assurant_listings", len(raw_rows))

    # Force mode needs the TTL set; default mode doesn't bother.
    fresh_bb_ids: set = set()
    if force:
        bb_ids = sorted({
            (m.get("bestbuy_listing_id") if m else None)
            for m in (_embedded_match(r) for r in raw_rows)
        } - {None})
        if bb_ids:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat()
            prices_resp = (
                client.table("bestbuy_prices")
                .select("bestbuy_listing_id,fetched_at")
                .in_("bestbuy_listing_id", bb_ids)
                .gt("fetched_at", cutoff)
                .execute()
            )
            for row in prices_resp.data or []:
                fresh_bb_ids.add(row["bestbuy_listing_id"])
            logger.info(
                "Forced-mode TTL check: %d of %d cached BB listings are fresh (< %dh)",
                len(fresh_bb_ids), len(bb_ids), ttl_hours,
            )

    out = _filter_eligible_listings(raw_rows, force=force, fresh_bb_ids=fresh_bb_ids)

    if force:
        logger.info(
            "Forced mode: scraping %d base candidates "
            "(match-presence filter bypassed, TTL=%dh)",
            len(out), ttl_hours,
        )
    else:
        logger.info("Scraping %d unmatched listings (default mode)", len(out))

    if limit is not None:
        before = len(out)
        out = out[:limit]
        logger.info("Limit applied: %d -> %d listings", before, len(out))

    return out


# --------------------------------------------------------------------------- #
# Read — single listing (operator dispatch path)
# --------------------------------------------------------------------------- #


def read_single_listing(
    client: Client,
    listing_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Fetch one assurant_listings row by listing_id, with the same flattened
    shape as read_biddable_listings_needing_bb_prices entries.

    No filtering: the operator may want to refresh a listing that is out of
    stock, has any grade, or is already matched. Returns None if not found.
    """
    resp = (
        client.table("assurant_listings")
        .select(_LISTING_SELECT_EXPR)
        .eq("listing_id", listing_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    return _flatten_row(rows[0])


# --------------------------------------------------------------------------- #
# Write — success path
# --------------------------------------------------------------------------- #


def _infer_bb_grade(name: str) -> Optional[str]:
    # Kept here so supabase_io doesn't import matcher. Same rules.
    import re
    if not name:
        return None
    nl = name.lower()
    if "excellent" in nl:
        return "Excellent"
    if re.search(r"\bgood\b", nl):
        return "Good"
    if re.search(r"\bfair\b", nl):
        return "Fair"
    return None


def _upsert_bestbuy_listing(
    client: Client,
    product_url: str,
    product_title: str,
    make: Optional[str],
    model: Optional[str],
    capacity: Optional[str],
    color: Optional[str],
) -> int:
    """Upsert by product_url. Returns bestbuy_listings.id."""
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "product_url": product_url,
        "product_title": product_title,
        "bb_grade": _infer_bb_grade(product_title),
        "make": make,
        "model": model,
        "capacity": capacity,
        "color": color,
        "last_verified_at": now_iso,
    }
    payload = {k: v for k, v in payload.items() if v is not None or k == "last_verified_at"}

    resp = (
        client.table("bestbuy_listings")
        .upsert(payload, on_conflict="product_url")
        .execute()
    )
    data = resp.data or []
    if data:
        return data[0]["id"]

    fetched = (
        client.table("bestbuy_listings")
        .select("id")
        .eq("product_url", product_url)
        .limit(1)
        .execute()
    )
    if fetched.data:
        return fetched.data[0]["id"]
    raise RuntimeError(f"Failed to upsert/fetch bestbuy_listings row for {product_url}")


def write_scrape_success(
    client: Client,
    listing_id: int,
    url: str,
    matched_bb_name: str,
    main_price: Optional[float],
    marketplace_prices: List[float],
    lowest_price: float,
    is_approximate: bool = False,
    buy_box_seller: Optional[str] = None,
    assurant_make: Optional[str] = None,
    assurant_model: Optional[str] = None,
    assurant_capacity: Optional[str] = None,
    assurant_color: Optional[str] = None,
    competing_offers_extras: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Best-effort across 3 calls:
      1. Upsert bestbuy_listings by product_url -> id
      2. Insert bestbuy_prices row (append-only)
      3. Upsert assurant_listing_matches

    `competing_offers_extras` (optional, additive): extra keys to merge into the
    competing_offers JSONB alongside the existing main_price / marketplace_prices /
    matched_name. The existing keys are preserved verbatim for Dashboard
    compatibility; extras are added next to them.
    """
    bb_id = _upsert_bestbuy_listing(
        client, product_url=url, product_title=matched_bb_name,
        make=assurant_make, model=assurant_model,
        capacity=assurant_capacity, color=assurant_color,
    )

    competing_offers: Dict[str, Any] = {
        "main_price": main_price,
        "marketplace_prices": marketplace_prices,
        "matched_name": matched_bb_name,
    }
    if competing_offers_extras:
        # Caller-supplied keys go alongside the base three. We intentionally let
        # caller extras win on key collisions so future scrapers can supply
        # richer values for main_price etc. without us renaming things here.
        competing_offers.update(competing_offers_extras)

    client.table("bestbuy_prices").insert({
        "bestbuy_listing_id": bb_id,
        "buy_box_price_cad": float(lowest_price),
        "buy_box_seller": buy_box_seller or "BestBuy",
        "competing_offers": competing_offers,
    }).execute()

    now_iso = datetime.now(timezone.utc).isoformat()
    confidence = "medium" if is_approximate else "high"
    client.table("assurant_listing_matches").upsert({
        "assurant_listing_id": listing_id,
        "bestbuy_listing_id": bb_id,
        "confidence": confidence,
        "matched_at": now_iso,
        "last_rematch_at": now_iso,
    }, on_conflict="assurant_listing_id").execute()


# --------------------------------------------------------------------------- #
# Write — failure path
# --------------------------------------------------------------------------- #


def write_scrape_failure(
    client: Client,
    listing_id: int,
    reason: str,
    url_attempted: Optional[str] = None,
    query: Optional[str] = None,
) -> None:
    """Log a per-listing scrape failure to agent_decisions."""
    client.table("agent_decisions").insert({
        "assurant_listing_id": listing_id,
        "decision": "bb_scrape_failed",
        "reasoning": (reason or "")[:500],
        "inputs_json": {
            "reason": reason,
            "url_attempted": url_attempted,
            "query": query,
        },
    }).execute()
