"""
Unit tests for the Weighted Confidence Index reconciliation engine.

Run with:
    python -m pytest test_reconciliation.py -v
"""

from __future__ import annotations

import math
import unittest
from datetime import date
from decimal import Decimal

from models import Invoice, Payment, ReconciliationResult
from reconciliation_service import (
    ReconciliationConfig,
    ReconciliationService,
    _levenshtein_distance,
    _levenshtein_similarity,
    _normalise,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_invoice(**overrides) -> Invoice:
    """Factory with sensible defaults; pass keyword overrides as needed."""
    defaults = dict(
        invoice_number="INV-2024-00142",
        amount=Decimal("10000.00"),
        issue_date=date(2024, 6, 1),
        due_date=date(2024, 7, 1),
        buyer_business_name="Acme Corp Ltd.",
        currency="USD",
    )
    defaults.update(overrides)
    return Invoice(**defaults)


def _make_payment(**overrides) -> Payment:
    """Factory with sensible defaults; pass keyword overrides as needed."""
    defaults = dict(
        reference="INV-2024-00142",
        amount=Decimal("10000.00"),
        payment_date=date(2024, 6, 15),
        sender_name="Acme Corp Ltd.",
        currency="USD",
    )
    defaults.update(overrides)
    return Payment(**defaults)


# ── Test: Levenshtein helpers ────────────────────────────────────────────────

class TestLevenshteinHelpers(unittest.TestCase):
    """Tests for the pure-Python Levenshtein utilities."""

    def test_identical_strings(self) -> None:
        self.assertEqual(_levenshtein_distance("abc", "abc"), 0)
        self.assertAlmostEqual(_levenshtein_similarity("abc", "abc"), 1.0)

    def test_completely_different(self) -> None:
        self.assertEqual(_levenshtein_distance("abc", "xyz"), 3)
        self.assertAlmostEqual(_levenshtein_similarity("abc", "xyz"), 0.0)

    def test_empty_strings(self) -> None:
        self.assertEqual(_levenshtein_distance("", ""), 0)
        self.assertAlmostEqual(_levenshtein_similarity("", ""), 1.0)

    def test_one_empty(self) -> None:
        self.assertEqual(_levenshtein_distance("hello", ""), 5)
        self.assertAlmostEqual(_levenshtein_similarity("hello", ""), 0.0)

    def test_single_edit(self) -> None:
        self.assertEqual(_levenshtein_distance("kitten", "sitten"), 1)
        self.assertAlmostEqual(
            _levenshtein_similarity("kitten", "sitten"), 5 / 6
        )


class TestNormalise(unittest.TestCase):
    """Tests for the string normalisation helper."""

    def test_strips_special_chars(self) -> None:
        self.assertEqual(_normalise("INV-2024/00142"), "inv202400142")

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(_normalise("  Acme   Corp  "), "acme corp")

    def test_lowercases(self) -> None:
        self.assertEqual(_normalise("HELLO"), "hello")


# ── Test: Amount scoring ─────────────────────────────────────────────────────

class TestAmountScore(unittest.TestCase):
    """Tests for the amount scoring vector."""

    def setUp(self) -> None:
        self.svc = ReconciliationService()

    def test_exact_match(self) -> None:
        inv = _make_invoice(amount=Decimal("5000.00"))
        pay = _make_payment(amount=Decimal("5000.00"))
        score, _ = self.svc.score_amount(inv, pay)
        self.assertAlmostEqual(score, 1.0)

    def test_within_tolerance(self) -> None:
        """Payment is 0.5 % less than invoice (bank fee deduction)."""
        inv = _make_invoice(amount=Decimal("10000.00"))
        pay = _make_payment(amount=Decimal("9950.00"))  # 0.5 % off
        score, _ = self.svc.score_amount(inv, pay)
        expected = 1.0 - (50.0 / 10000.0)  # 0.995
        self.assertAlmostEqual(score, expected)

    def test_exactly_at_tolerance_boundary(self) -> None:
        """Payment deviates by exactly 1 %."""
        inv = _make_invoice(amount=Decimal("10000.00"))
        pay = _make_payment(amount=Decimal("9900.00"))
        score, _ = self.svc.score_amount(inv, pay)
        expected = 1.0 - (100.0 / 10000.0)  # 0.99
        self.assertAlmostEqual(score, expected)

    def test_outside_tolerance(self) -> None:
        """Payment is 5 % off — should score 0."""
        inv = _make_invoice(amount=Decimal("10000.00"))
        pay = _make_payment(amount=Decimal("9500.00"))
        score, _ = self.svc.score_amount(inv, pay)
        self.assertAlmostEqual(score, 0.0)

    def test_overpayment_within_tolerance(self) -> None:
        """Slight overpayment should still be within tolerance."""
        inv = _make_invoice(amount=Decimal("10000.00"))
        pay = _make_payment(amount=Decimal("10050.00"))  # +0.5 %
        score, _ = self.svc.score_amount(inv, pay)
        expected = 1.0 - (50.0 / 10000.0)
        self.assertAlmostEqual(score, expected)


# ── Test: Reference scoring ──────────────────────────────────────────────────

class TestReferenceScore(unittest.TestCase):
    """Tests for the reference / PSID scoring vector."""

    def setUp(self) -> None:
        self.svc = ReconciliationService()

    def test_exact_match(self) -> None:
        inv = _make_invoice(invoice_number="INV-2024-00142")
        pay = _make_payment(reference="INV-2024-00142")
        score, _ = self.svc.score_reference(inv, pay)
        self.assertAlmostEqual(score, 1.0)

    def test_case_insensitive_match(self) -> None:
        inv = _make_invoice(invoice_number="INV-2024-00142")
        pay = _make_payment(reference="inv-2024-00142")
        score, _ = self.svc.score_reference(inv, pay)
        self.assertAlmostEqual(score, 1.0)

    def test_special_chars_stripped(self) -> None:
        """Dashes and slashes are stripped; should still match."""
        inv = _make_invoice(invoice_number="INV/2024/00142")
        pay = _make_payment(reference="INV-2024-00142")
        score, _ = self.svc.score_reference(inv, pay)
        self.assertAlmostEqual(score, 1.0)

    def test_partial_match(self) -> None:
        inv = _make_invoice(invoice_number="INV-2024-00142")
        pay = _make_payment(reference="INV-2024-00143")  # last digit differs
        score, _ = self.svc.score_reference(inv, pay)
        # One character difference out of 13 → ~0.923
        self.assertGreater(score, 0.85)
        self.assertLess(score, 1.0)

    def test_completely_different(self) -> None:
        inv = _make_invoice(invoice_number="INV-2024-00142")
        pay = _make_payment(reference="PAYMENT-XYZ-9999")
        score, _ = self.svc.score_reference(inv, pay)
        self.assertLess(score, 0.5)


# ── Test: Timing scoring ─────────────────────────────────────────────────────

class TestTimingScore(unittest.TestCase):
    """Tests for the timing / date scoring vector."""

    def setUp(self) -> None:
        self.svc = ReconciliationService()

    def test_same_day(self) -> None:
        inv = _make_invoice(issue_date=date(2024, 6, 1))
        pay = _make_payment(payment_date=date(2024, 6, 1))
        score, _ = self.svc.score_timing(inv, pay)
        self.assertAlmostEqual(score, 1.0)

    def test_payment_before_invoice(self) -> None:
        """Pre-dated payments should score 0."""
        inv = _make_invoice(issue_date=date(2024, 6, 1))
        pay = _make_payment(payment_date=date(2024, 5, 28))
        score, _ = self.svc.score_timing(inv, pay)
        self.assertAlmostEqual(score, 0.0)

    def test_payment_14_days_later(self) -> None:
        inv = _make_invoice(issue_date=date(2024, 6, 1))
        pay = _make_payment(payment_date=date(2024, 6, 15))
        score, _ = self.svc.score_timing(inv, pay)
        expected = math.exp(-0.05 * 14)
        self.assertAlmostEqual(score, expected, places=4)

    def test_payment_beyond_max_days(self) -> None:
        """Payment 200 days later exceeds the 180-day window."""
        inv = _make_invoice(issue_date=date(2024, 1, 1))
        pay = _make_payment(payment_date=date(2024, 7, 20))  # 201 days
        score, _ = self.svc.score_timing(inv, pay)
        self.assertAlmostEqual(score, 0.0)

    def test_exponential_decay_ordering(self) -> None:
        """Earlier payments must score higher than later ones."""
        inv = _make_invoice(issue_date=date(2024, 6, 1))
        pay_early = _make_payment(payment_date=date(2024, 6, 5))
        pay_late = _make_payment(payment_date=date(2024, 7, 15))

        score_early, _ = self.svc.score_timing(inv, pay_early)
        score_late, _ = self.svc.score_timing(inv, pay_late)
        self.assertGreater(score_early, score_late)


# ── Test: Entity scoring ─────────────────────────────────────────────────────

class TestEntityScore(unittest.TestCase):
    """Tests for the entity / business-name scoring vector."""

    def setUp(self) -> None:
        self.svc = ReconciliationService()

    def test_exact_match(self) -> None:
        inv = _make_invoice(buyer_business_name="Acme Corp Ltd.")
        pay = _make_payment(sender_name="Acme Corp Ltd.")
        score, _ = self.svc.score_entity(inv, pay)
        self.assertAlmostEqual(score, 1.0)

    def test_case_insensitive(self) -> None:
        inv = _make_invoice(buyer_business_name="Acme Corp Ltd.")
        pay = _make_payment(sender_name="acme corp ltd.")
        score, _ = self.svc.score_entity(inv, pay)
        self.assertAlmostEqual(score, 1.0)

    def test_minor_typo(self) -> None:
        inv = _make_invoice(buyer_business_name="Acme Corporation")
        pay = _make_payment(sender_name="Acme Corporaton")  # typo
        score, _ = self.svc.score_entity(inv, pay)
        self.assertGreater(score, 0.9)

    def test_abbreviation(self) -> None:
        inv = _make_invoice(buyer_business_name="Acme Corporation Ltd")
        pay = _make_payment(sender_name="Acme Corp")
        score, _ = self.svc.score_entity(inv, pay)
        # Abbreviation loses several characters → moderate similarity
        self.assertGreater(score, 0.4)
        self.assertLess(score, 0.8)

    def test_completely_different(self) -> None:
        inv = _make_invoice(buyer_business_name="Acme Corp")
        pay = _make_payment(sender_name="Globex Industries")
        score, _ = self.svc.score_entity(inv, pay)
        self.assertLess(score, 0.4)


# ── Test: End-to-end reconciliation ──────────────────────────────────────────

class TestReconciliation(unittest.TestCase):
    """Integration tests for the full reconciliation pipeline."""

    def setUp(self) -> None:
        self.svc = ReconciliationService()

    def test_perfect_match(self) -> None:
        """All vectors align perfectly — WCI should be ≈ 1.0."""
        inv = _make_invoice(issue_date=date(2024, 6, 1))
        pay = _make_payment(payment_date=date(2024, 6, 1))  # same day

        result = self.svc.reconcile(inv, pay)

        self.assertAlmostEqual(result.amount_score, 1.0)
        self.assertAlmostEqual(result.reference_score, 1.0)
        self.assertAlmostEqual(result.timing_score, 1.0)
        self.assertAlmostEqual(result.entity_score, 1.0)
        self.assertAlmostEqual(result.weighted_confidence_index, 1.0)
        self.assertTrue(result.is_confident_match)
        self.assertTrue(result.is_probable_match)

    def test_good_match_with_minor_deviations(self) -> None:
        """Slightly off amount and 14-day delay — still a confident match."""
        inv = _make_invoice(
            amount=Decimal("10000.00"),
            issue_date=date(2024, 6, 1),
        )
        pay = _make_payment(
            amount=Decimal("9950.00"),  # -0.5 %
            payment_date=date(2024, 6, 15),  # +14 days
        )

        result = self.svc.reconcile(inv, pay)

        self.assertGreater(result.weighted_confidence_index, 0.85)
        self.assertTrue(result.is_confident_match)

    def test_partial_match(self) -> None:
        """Different entity and slightly off reference — probable match."""
        inv = _make_invoice(
            buyer_business_name="Acme Corporation Ltd",
            invoice_number="INV-2024-00142",
            issue_date=date(2024, 6, 1),
        )
        pay = _make_payment(
            sender_name="Acme Corp",
            reference="INV-2024-00143",  # one digit off
            payment_date=date(2024, 6, 10),
        )

        result = self.svc.reconcile(inv, pay)

        self.assertGreater(result.weighted_confidence_index, 0.65)
        self.assertTrue(result.is_probable_match)

    def test_no_match(self) -> None:
        """Everything is different — should be a clear non-match."""
        inv = _make_invoice(
            invoice_number="INV-2024-00142",
            amount=Decimal("10000.00"),
            issue_date=date(2024, 6, 1),
            buyer_business_name="Acme Corp",
        )
        pay = _make_payment(
            reference="PAY-9999-ZZZZZ",
            amount=Decimal("500.00"),
            payment_date=date(2024, 5, 1),  # before invoice
            sender_name="Globex Industries",
        )

        result = self.svc.reconcile(inv, pay)

        self.assertLess(result.weighted_confidence_index, 0.3)
        self.assertFalse(result.is_confident_match)
        self.assertFalse(result.is_probable_match)

    def test_wci_is_clamped(self) -> None:
        """WCI must always lie within [0.0, 1.0]."""
        inv = _make_invoice()
        pay = _make_payment()
        result = self.svc.reconcile(inv, pay)
        self.assertGreaterEqual(result.weighted_confidence_index, 0.0)
        self.assertLessEqual(result.weighted_confidence_index, 1.0)

    def test_result_contains_details(self) -> None:
        """Every scoring vector should provide a human-readable explanation."""
        inv = _make_invoice()
        pay = _make_payment()
        result = self.svc.reconcile(inv, pay)

        for key in ("amount", "reference", "timing", "entity"):
            self.assertIn(key, result.details)
            self.assertIsInstance(result.details[key], str)
            self.assertTrue(len(result.details[key]) > 0)


# ── Test: Configuration ──────────────────────────────────────────────────────

class TestReconciliationConfig(unittest.TestCase):
    """Tests for the ReconciliationConfig dataclass."""

    def test_default_weights_sum_to_one(self) -> None:
        cfg = ReconciliationConfig()
        total = (
            cfg.amount_weight
            + cfg.reference_weight
            + cfg.timing_weight
            + cfg.entity_weight
        )
        self.assertAlmostEqual(total, 1.0)

    def test_invalid_weights_raise(self) -> None:
        with self.assertRaises(ValueError):
            ReconciliationConfig(amount_weight=0.5, reference_weight=0.5)

    def test_custom_tolerance(self) -> None:
        """A 2 % tolerance should pass amounts that 1 % would reject."""
        cfg = ReconciliationConfig(amount_tolerance_pct=0.02)
        svc = ReconciliationService(config=cfg)

        inv = _make_invoice(amount=Decimal("10000.00"))
        pay = _make_payment(amount=Decimal("9850.00"))  # 1.5 % off

        score, _ = svc.score_amount(inv, pay)
        self.assertGreater(score, 0.0)  # within 2 % tolerance


# ── Test: Model validation ───────────────────────────────────────────────────

class TestModelValidation(unittest.TestCase):
    """Tests for Invoice and Payment data validation."""

    def test_invoice_rejects_zero_amount(self) -> None:
        with self.assertRaises(ValueError):
            _make_invoice(amount=Decimal("0.00"))

    def test_invoice_rejects_negative_amount(self) -> None:
        with self.assertRaises(ValueError):
            _make_invoice(amount=Decimal("-100.00"))

    def test_payment_rejects_zero_amount(self) -> None:
        with self.assertRaises(ValueError):
            _make_payment(amount=Decimal("0.00"))

    def test_payment_rejects_negative_amount(self) -> None:
        with self.assertRaises(ValueError):
            _make_payment(amount=Decimal("-100.00"))


if __name__ == "__main__":
    unittest.main()
