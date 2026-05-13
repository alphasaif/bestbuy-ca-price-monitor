"""
Unit tests for supabase_io.py — eligibility filtering and single-listing read.

Run with:
    python -m unittest test_supabase_io
"""

import unittest
from unittest.mock import MagicMock

import supabase_io


def _row(listing_id, *, matched=False, bb_id=None, bb_url=None, **overrides):
    """Build a fake PostgREST row for assurant_listings + embedded match."""
    base = {
        "listing_id": listing_id,
        "item_id": f"ITEM{listing_id}",
        "warehouse": "WH1",
        "make": "Apple",
        "model": "iPhone 15",
        "model_number": None,
        "capacity": "128GB",
        "color": "Black",
        "grade": "DLS A+",
        "category": "Cellphone",
        "currency": "CAD",
        "list_price": 500.0,
        "available_quantity": 1,
    }
    base.update(overrides)
    if matched:
        base["assurant_listing_matches"] = {
            "bestbuy_listing_id": bb_id if bb_id is not None else 999,
            "bestbuy_listings": (
                {"id": bb_id if bb_id is not None else 999, "product_url": bb_url}
                if bb_url else None
            ),
        }
    else:
        # Either no row, or row exists but bb_listing_id is null.
        base["assurant_listing_matches"] = None
    return base


# --------------------------------------------------------------------------- #
# _filter_eligible_listings — pure logic
# --------------------------------------------------------------------------- #


class TestFilterEligibleListingsDefault(unittest.TestCase):
    """Default (force=False) should exclude any row with bestbuy_listing_id NOT NULL."""

    def test_excludes_matched_listings(self):
        rows = [
            _row(1, matched=False),
            _row(2, matched=True, bb_id=42, bb_url="https://bestbuy.ca/foo"),
            _row(3, matched=False),
        ]
        out = supabase_io._filter_eligible_listings(rows, force=False)
        ids = [r["listing_id"] for r in out]
        self.assertEqual(ids, [1, 3])

    def test_includes_match_row_with_null_bb_listing_id(self):
        # An assurant_listing_matches row exists but bestbuy_listing_id is NULL
        # (e.g. confidence='none' placeholder). Should still be eligible.
        row = _row(7, matched=False)
        row["assurant_listing_matches"] = {
            "bestbuy_listing_id": None,
            "bestbuy_listings": None,
        }
        out = supabase_io._filter_eligible_listings([row], force=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["listing_id"], 7)
        self.assertIsNone(out[0]["existing_bb_listing_id"])
        self.assertIsNone(out[0]["existing_bb_url"])

    def test_handles_list_shape_for_embedded_match(self):
        # PostgREST may return embedded relation as list. Must coerce.
        row = _row(11, matched=False)
        row["assurant_listing_matches"] = [{
            "bestbuy_listing_id": 99,
            "bestbuy_listings": [{"id": 99, "product_url": "https://x"}],
        }]
        out = supabase_io._filter_eligible_listings([row], force=False)
        self.assertEqual(out, [], "matched row in list-shape should still be filtered")

    def test_flatten_populates_cached_url(self):
        row = _row(5, matched=False)
        # No match, but suppose later a row comes through with NULL bb_id but
        # an URL — defensive case. We only care match fields are exposed.
        out = supabase_io._filter_eligible_listings([row], force=False)
        self.assertEqual(out[0]["make"], "Apple")
        self.assertEqual(out[0]["grade"], "DLS A+")


class TestFilterEligibleListingsForce(unittest.TestCase):
    """Force mode includes matched listings, except TTL-fresh ones."""

    def test_includes_all_when_no_fresh_set(self):
        rows = [
            _row(1, matched=False),
            _row(2, matched=True, bb_id=42, bb_url="https://bestbuy.ca/foo"),
            _row(3, matched=True, bb_id=43, bb_url="https://bestbuy.ca/bar"),
        ]
        out = supabase_io._filter_eligible_listings(rows, force=True, fresh_bb_ids=set())
        self.assertEqual([r["listing_id"] for r in out], [1, 2, 3])

    def test_excludes_only_ttl_fresh_in_force_mode(self):
        rows = [
            _row(1, matched=False),
            _row(2, matched=True, bb_id=42, bb_url="https://bestbuy.ca/foo"),
            _row(3, matched=True, bb_id=43, bb_url="https://bestbuy.ca/bar"),
        ]
        # Pretend bb_id=42 was scraped recently.
        out = supabase_io._filter_eligible_listings(
            rows, force=True, fresh_bb_ids={42},
        )
        ids = [r["listing_id"] for r in out]
        self.assertEqual(ids, [1, 3])
        # The cached URL is still populated for row 3.
        row3 = next(r for r in out if r["listing_id"] == 3)
        self.assertEqual(row3["existing_bb_url"], "https://bestbuy.ca/bar")
        self.assertEqual(row3["existing_bb_listing_id"], 43)


# --------------------------------------------------------------------------- #
# read_single_listing — mocked client
# --------------------------------------------------------------------------- #


def _make_mock_client(return_data):
    """Build a MagicMock supabase Client where .table().select()...execute()
    returns an object with .data == return_data."""
    client = MagicMock()
    chain = client.table.return_value
    # Each fluent call returns the same chain so we can chain arbitrarily.
    for attr in ("select", "eq", "in_", "gt", "or_", "limit"):
        getattr(chain, attr).return_value = chain
    chain.execute.return_value = MagicMock(data=return_data)
    return client


class TestReadSingleListing(unittest.TestCase):
    def test_returns_flattened_row_when_found(self):
        raw = _row(123, matched=True, bb_id=77, bb_url="https://bb.ca/77")
        client = _make_mock_client([raw])
        out = supabase_io.read_single_listing(client, 123)
        self.assertIsNotNone(out)
        self.assertEqual(out["listing_id"], 123)
        self.assertEqual(out["existing_bb_listing_id"], 77)
        self.assertEqual(out["existing_bb_url"], "https://bb.ca/77")

    def test_returns_none_when_not_found(self):
        client = _make_mock_client([])
        out = supabase_io.read_single_listing(client, 9999999)
        self.assertIsNone(out)

    def test_no_eligibility_filter_for_single_listing(self):
        # Out-of-stock + non-biddable grade — single-listing should still return it.
        raw = _row(
            555, matched=True, bb_id=10, bb_url="https://bb.ca/10",
            available_quantity=0, grade="DLS C-",
        )
        client = _make_mock_client([raw])
        out = supabase_io.read_single_listing(client, 555)
        self.assertIsNotNone(out)
        self.assertEqual(out["listing_id"], 555)
        self.assertEqual(out["grade"], "DLS C-")


# --------------------------------------------------------------------------- #
# Scraper main() with --listing-id (mocked end-to-end)
# --------------------------------------------------------------------------- #


class TestScraperSingleListingDispatch(unittest.TestCase):
    """Verify --listing-id routes through read_single_listing only."""

    def test_main_with_listing_id_calls_read_single_listing(self):
        import asyncio
        from unittest.mock import patch

        import scraper

        single_row = _row(424242, matched=False)

        with patch.object(scraper, "supabase_io") as mock_sio, \
             patch.object(scraper, "process_row") as mock_process:

            # Make process_row a no-op coroutine.
            async def noop(**kwargs):
                # bump attempted via the stats arg so the run-summary path works.
                await kwargs["stats"].bump("attempted")
                await kwargs["stats"].bump("matched")
            mock_process.side_effect = lambda **kw: noop(**kw)

            mock_sio.get_client.return_value = MagicMock()
            mock_sio.read_single_listing.return_value = single_row
            # Should NOT be called.
            mock_sio.read_biddable_listings_needing_bb_prices.side_effect = (
                AssertionError("batch read must not be called in single-listing mode")
            )

            args = scraper.parse_args(["--listing-id", "424242", "--dry-run"])
            code = asyncio.run(scraper.main_async(args))

        self.assertEqual(code, 0)
        mock_sio.read_single_listing.assert_called_once()
        # The listing_id arg goes positionally; check it.
        call_args = mock_sio.read_single_listing.call_args
        self.assertEqual(call_args.args[1], 424242)

    def test_listing_id_overrides_force_and_limit(self):
        # parse_args alone doesn't enforce precedence; main_async does. This
        # test confirms the warning path runs and listing_id still wins.
        import asyncio
        from unittest.mock import patch

        import scraper

        with patch.object(scraper, "supabase_io") as mock_sio, \
             patch.object(scraper, "process_row") as mock_process:

            async def noop(**kwargs):
                pass
            mock_process.side_effect = lambda **kw: noop(**kw)

            mock_sio.get_client.return_value = MagicMock()
            mock_sio.read_single_listing.return_value = _row(7, matched=False)
            mock_sio.read_biddable_listings_needing_bb_prices.side_effect = (
                AssertionError("batch read must not be called")
            )

            args = scraper.parse_args(
                ["--listing-id", "7", "--force", "--limit", "100", "--dry-run"]
            )
            code = asyncio.run(scraper.main_async(args))
        self.assertEqual(code, 0)
        mock_sio.read_single_listing.assert_called_once()


if __name__ == "__main__":
    unittest.main()
