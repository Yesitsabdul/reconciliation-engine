"""Unit tests for bulk_match_engine."""

import unittest
from datetime import date
from decimal import Decimal

from invoice_dal import CandidateInvoice
from bulk_match_engine import BulkMatchResult, find_bulk_combinations


def _inv(id, number, amount, buyer_name="Acme Corp", buyer_code="ACME01"):
    return CandidateInvoice(
        invoice_id=id, invoice_number=number,
        buyer_business_name=buyer_name, buyer_iden_code=buyer_code,
        issue_date=date(2024, 6, 1), due_date=date(2024, 7, 1),
        currency="USD", total_sales_value=Decimal(str(amount)),
    )


class TestBulkMatchEngine(unittest.TestCase):
    """Tests for find_bulk_combinations."""

    def test_exact_match_two_invoices(self):
        """Two invoices summing exactly to target → 1 result with 0.0 deviation."""
        invoices = [_inv(1, "INV-1", 500), _inv(2, "INV-2", 500)]
        results = find_bulk_combinations(invoices, Decimal("1000"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].combined_amount, Decimal("1000"))
        self.assertAlmostEqual(results[0].deviation_pct, 0.0)

    def test_exact_match_five_invoices(self):
        """Five invoices summing to target → found."""
        invoices = [
            _inv(1, "INV-1", 200),
            _inv(2, "INV-2", 200),
            _inv(3, "INV-3", 200),
            _inv(4, "INV-4", 200),
            _inv(5, "INV-5", 200),
        ]
        results = find_bulk_combinations(invoices, Decimal("1000"))
        # The 5-invoice combo should be among the results
        five_inv = [r for r in results if len(r.invoices) == 5]
        self.assertTrue(len(five_inv) >= 1)
        self.assertEqual(five_inv[0].combined_amount, Decimal("1000"))

    def test_six_invoices_needed_no_match(self):
        """Only solvable with 6+ invoices → no results with max_group=5."""
        invoices = [_inv(i, f"INV-{i}", 100) for i in range(1, 7)]
        results = find_bulk_combinations(invoices, Decimal("600"), max_group=5)
        self.assertEqual(len(results), 0)

    def test_within_tolerance(self):
        """Sum is 0.3% off target → match found (default tolerance 0.5%)."""
        # 500 + 500 = 1000; target = 1003 → deviation = 0.3%
        invoices = [_inv(1, "INV-1", 500), _inv(2, "INV-2", 500)]
        results = find_bulk_combinations(invoices, Decimal("1003"))
        self.assertTrue(len(results) >= 1)
        self.assertAlmostEqual(results[0].deviation_pct, 3 / 1003, places=5)

    def test_outside_tolerance(self):
        """Sum is ~1% off target → no match (default tolerance 0.5%)."""
        # 500 + 500 = 1000; target = 1010 → deviation ≈ 0.99%
        invoices = [_inv(1, "INV-1", 500), _inv(2, "INV-2", 500)]
        results = find_bulk_combinations(invoices, Decimal("1010"))
        self.assertEqual(len(results), 0)

    def test_single_invoice_no_match(self):
        """Only 1 invoice matches target → no result (min_group=2)."""
        invoices = [_inv(1, "INV-1", 1000)]
        results = find_bulk_combinations(invoices, Decimal("1000"))
        self.assertEqual(len(results), 0)

    def test_different_buyers_separate_groups(self):
        """Invoices from 2 buyers; only one buyer's invoices sum correctly."""
        invoices = [
            _inv(1, "INV-1", 400, buyer_name="Acme Corp", buyer_code="ACME01"),
            _inv(2, "INV-2", 600, buyer_name="Acme Corp", buyer_code="ACME01"),
            _inv(3, "INV-3", 300, buyer_name="Beta Inc", buyer_code="BETA01"),
            _inv(4, "INV-4", 100, buyer_name="Beta Inc", buyer_code="BETA01"),
        ]
        results = find_bulk_combinations(invoices, Decimal("1000"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].buyer_identifier, "ACME01")

    def test_empty_input(self):
        """Empty invoice list → no results."""
        results = find_bulk_combinations([], Decimal("1000"))
        self.assertEqual(len(results), 0)

    def test_multiple_combinations_sorted(self):
        """Two valid combos — closer deviation first."""
        invoices = [
            _inv(1, "INV-1", 500),
            _inv(2, "INV-2", 500),
            _inv(3, "INV-3", 502),
        ]
        # target = 1001: combo (500+500)=1000 dev≈0.1%, combo (500+502)=1002 dev≈0.1%
        # (500,500) → |1000-1001|/1001 ≈ 0.0999%
        # (500,502) → |1002-1001|/1001 ≈ 0.0999%
        # (500,502) → closer? Let's pick a target that differentiates clearly.
        # target = 1002: (500+500)=1000 dev≈0.1996%, (500+502)=1002 dev=0%
        results = find_bulk_combinations(invoices, Decimal("1002"))
        self.assertTrue(len(results) >= 2)
        # First result should have the smallest deviation
        self.assertLessEqual(results[0].deviation_pct, results[1].deviation_pct)
        # The exact match should be first
        self.assertAlmostEqual(results[0].deviation_pct, 0.0)

    def test_buyer_code_fallback(self):
        """When buyer_iden_code is empty, groups by buyer_business_name."""
        invoices = [
            _inv(1, "INV-1", 400, buyer_name="Acme Corp", buyer_code=""),
            _inv(2, "INV-2", 600, buyer_name="Acme Corp", buyer_code=""),
        ]
        results = find_bulk_combinations(invoices, Decimal("1000"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].buyer_identifier, "Acme Corp")


if __name__ == "__main__":
    unittest.main()
