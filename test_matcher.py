"""
Unit tests for matcher.py hard-fail rules.

Focus: regressions surfaced by the 2026-04-24 full-batch run that produced
0/24 matches due to two bugs — storage 1024GB/1TB equivalence missing, and
strict-keyword asymmetric token-set vs substring matching.

Run with:
    python -m unittest test_matcher
"""

import unittest

from matcher import (
    Candidate,
    SearchTerms,
    _canonicalize_storage_token,
    _check_fan_edition_mismatch,
    _check_strict_keyword_mismatch,
    _check_version_suffix_mismatch,
    _is_accessory,
    dls_to_matcher_grade,
    normalize_storage,
    score_candidate,
)


class TestNormalizeStorage(unittest.TestCase):
    def test_gb_passes_through(self):
        self.assertEqual(normalize_storage("256GB"), "256GB")
        self.assertEqual(normalize_storage("128 GB"), "128GB")

    def test_tb_canonicalises_to_gb(self):
        self.assertEqual(normalize_storage("1TB"), "1024GB")
        self.assertEqual(normalize_storage("1 tb"), "1024GB")
        self.assertEqual(normalize_storage("2TB"), "2048GB")

    def test_already_canonical_1024gb(self):
        self.assertEqual(normalize_storage("1024GB"), "1024GB")


class TestCanonicalizeStorageToken(unittest.TestCase):
    def test_tb_vs_gb_compare_equal(self):
        self.assertEqual(
            _canonicalize_storage_token("1tb"),
            _canonicalize_storage_token("1024gb"),
        )
        self.assertEqual(
            _canonicalize_storage_token("2tb"),
            _canonicalize_storage_token("2048gb"),
        )

    def test_distinct_storages_stay_distinct(self):
        self.assertNotEqual(
            _canonicalize_storage_token("256gb"),
            _canonicalize_storage_token("512gb"),
        )
        self.assertNotEqual(
            _canonicalize_storage_token("1tb"),
            _canonicalize_storage_token("256gb"),
        )


def _terms(make, model, capacity, color=None, category="Cellphone"):
    return SearchTerms(
        brand=make, model=model,
        storage=normalize_storage(capacity),
        color=color, device_type=category,
    )


class TestStorageHardFail(unittest.TestCase):
    """1024GB upstream vs 1TB BestBuy title regression."""

    def test_iphone_15_pro_max_1024gb_matches_bb_1tb_title(self):
        terms = _terms("Apple", "iPhone 15 Pro Max", "1024GB", "Blue Titanium")
        cand = Candidate(
            name="Refurbished (Excellent) - Apple iPhone 15 Pro Max 1TB - Blue Titanium - Unlocked",
            price=1200.0,
            url="https://www.bestbuy.ca/en-ca/product/foo/16234567",
        )
        score = score_candidate(cand, terms, grade="Excellent")
        self.assertGreater(score, 5, f"expected positive match score, got {score}")

    def test_galaxy_s25_ultra_1024gb_matches_bb_1tb_title(self):
        terms = _terms("Samsung", "Galaxy S25 Ultra 5G", "1024GB", "Titanium Jade Green")
        cand = Candidate(
            name="Refurbished (Excellent) - Samsung Galaxy S25 Ultra 5G 1TB - Titanium Jade Green - Unlocked",
            price=1500.0,
            url="https://www.bestbuy.ca/en-ca/product/bar/19000001",
        )
        score = score_candidate(cand, terms, grade="Excellent")
        self.assertGreater(score, 5)

    def test_wrong_storage_still_hard_fails(self):
        terms = _terms("Apple", "iPhone 15 Pro Max", "256GB", "Blue Titanium")
        cand = Candidate(
            name="Refurbished (Excellent) - Apple iPhone 15 Pro Max 1TB - Blue Titanium - Unlocked",
            price=1200.0,
            url="https://www.bestbuy.ca/en-ca/product/foo/16234567",
        )
        score = score_candidate(cand, terms, grade="Excellent")
        self.assertEqual(score, -500, "256GB ≠ 1TB must still hard-fail")


class TestStrictKeywordSymmetry(unittest.TestCase):
    """Strict keyword symmetry regression tests."""

    def test_flip6_token_concat_does_not_fail_strict_keyword(self):
        # Before fix: model_words={"galaxy","z","flip6"}, "flip" not in set
        # but "flip" in BB title → asymmetric → hard-fail.
        self.assertFalse(
            _check_strict_keyword_mismatch(
                "Galaxy Z Flip6",
                "Refurbished (Good) - Samsung Galaxy Z Flip6 256GB - Blue - Unlocked",
            )
        )

    def test_flip5_token_concat_does_not_fail(self):
        self.assertFalse(
            _check_strict_keyword_mismatch(
                "Galaxy Z Flip5",
                "Refurbished (Good) - Samsung Galaxy Z Flip5 512GB - Mint - Unlocked",
            )
        )

    def test_fold6_token_concat_does_not_fail(self):
        self.assertFalse(
            _check_strict_keyword_mismatch(
                "Galaxy Z Fold6",
                "Refurbished (Good) - Samsung Galaxy Z Fold6 512GB - Pink - Unlocked",
            )
        )

    def test_ultramarine_color_does_not_match_ultra_keyword(self):
        # Before fix: "ultra" substring-in "ultramarine" color name triggered
        # in_name=True; model didn't contain ultra → mismatch → hard-fail.
        self.assertFalse(
            _check_strict_keyword_mismatch(
                "iPhone 16 Plus",
                "Refurbished (Good) - Apple iPhone 16 Plus 256GB - Ultramarine - Unlocked",
            )
        )

    def test_legitimate_ultra_mismatch_still_detected(self):
        # Upstream says plain S24, BB says S24 Ultra → legit higher-tier
        # mismatch, still hard-fail.
        self.assertTrue(
            _check_strict_keyword_mismatch(
                "Galaxy S24",
                "Refurbished (Good) - Samsung Galaxy S24 Ultra 256GB - Black",
            )
        )

    def test_legitimate_plus_mismatch_still_detected(self):
        self.assertTrue(
            _check_strict_keyword_mismatch(
                "iPhone 16",
                "Refurbished (Good) - Apple iPhone 16 Plus 128GB - Ultramarine",
            )
        )


class TestFanEditionStillWorks(unittest.TestCase):
    """Regression guard: Fan Edition hard-fail from commit a66ea44."""

    def test_fan_edition_rejects_plain_candidate(self):
        self.assertTrue(
            _check_fan_edition_mismatch(
                "Galaxy S23 Fan Edition 5G",
                "Refurbished (Good) - Samsung Galaxy S23 128GB - Cream - Unlocked",
            )
        )

    def test_fan_edition_accepts_fe_candidate(self):
        self.assertFalse(
            _check_fan_edition_mismatch(
                "Galaxy S23 Fan Edition 5G",
                "Refurbished (Good) - Samsung Galaxy S23 FE 128GB - Mint - Unlocked",
            )
        )


class TestVersionSuffixStillWorks(unittest.TestCase):
    """Regression guard: version-suffix hard-fail from commit a66ea44."""

    def test_lg_x_power_2_rejects_x_power_candidate(self):
        # LG X Power 2 vs BB "LG X Power" — should hard-fail.
        self.assertTrue(
            _check_version_suffix_mismatch(
                "LG", "X Power 2",
                "Refurbished (Good) - LG X Power 16GB - Black",
            )
        )

    def test_iphone_15_not_affected_by_version_check(self):
        # "iPhone" is in VERSION_CHECK_SKIP_PATTERNS — trailing integer
        # is a series marker, not a version.
        self.assertFalse(
            _check_version_suffix_mismatch(
                "Apple", "iPhone 15",
                "Refurbished (Excellent) - Apple iPhone 14 128GB - Black",
            )
        )


class TestAccessoryFilterStillWorks(unittest.TestCase):
    """Regression guard: accessory URL / keyword filter from commit a66ea44."""

    def test_battery_pack_is_accessory(self):
        self.assertTrue(
            _is_accessory(
                "Samsung Battery Pack 10000mAh - Black",
                "https://www.bestbuy.ca/en-ca/product/foo",
            )
        )

    def test_accessory_url_path_catches_plain_title(self):
        self.assertTrue(
            _is_accessory(
                "Samsung Ambiguous Product",
                "https://www.bestbuy.ca/en-ca/category/accessories/foo",
            )
        )

    def test_phone_is_not_accessory(self):
        self.assertFalse(
            _is_accessory(
                "Refurbished (Good) - Samsung Galaxy S24 128GB - Gray - Unlocked",
                "https://www.bestbuy.ca/en-ca/product/refurbished-good-samsung-galaxy-s24-128gb-gray-unlocked/16000000",
            )
        )


class TestDlsGradeMapping(unittest.TestCase):
    def test_a_plus_to_excellent(self):
        self.assertEqual(dls_to_matcher_grade("DLS A+"), "Excellent")

    def test_b_plus_to_good(self):
        self.assertEqual(dls_to_matcher_grade("DLS B+"), "Good")


if __name__ == "__main__":
    unittest.main()
