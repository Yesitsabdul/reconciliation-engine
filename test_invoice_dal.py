"""
Unit tests for the Invoice Data Access Layer.

Uses an in-memory SQLite database so tests run without a MySQL server.
SQLite is wire-compatible enough with SQLAlchemy for functional testing;
MySQL-specific features (SOUNDEX, full-text) are not exercised here.

Run with:
    python -m pytest test_invoice_dal.py -v
    python -m unittest test_invoice_dal -v
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from db_models import Base, InvoiceDetailRow, InvoiceRow
from invoice_dal import (
    CandidateInvoice,
    InvoiceDAL,
    InvoiceDetailDTO,
    match_payment,
)


# ── Test fixtures ────────────────────────────────────────────────────────────

def _seed_database(session: Session) -> None:
    """Populate the in-memory DB with representative test data.

    Creates 5 invoices across two companies (NFL, NBA) with varying
    statuses and amounts so that every filter path is exercised.
    """

    # ── Invoice 1: NFL, Unpaid, Acme Corp, $10 000 ──────────────────────
    inv1 = InvoiceRow(
        id=1,
        invoice_number="INV-2024-00142",
        company_code="NFL",
        status="Unpaid",
        buyer_business_name="Acme Corp Ltd.",
        issue_date=date(2024, 6, 1),
        due_date=date(2024, 7, 1),
        currency="USD",
    )
    session.add(inv1)
    session.flush()
    session.add_all([
        InvoiceDetailRow(
            id=1, invoice_id=1, product_code="PROD-A",
            quantity=100, unit_price=Decimal("50.00"), sales_value=Decimal("5000.00"),
        ),
        InvoiceDetailRow(
            id=2, invoice_id=1, product_code="PROD-B",
            quantity=50, unit_price=Decimal("100.00"), sales_value=Decimal("5000.00"),
        ),
    ])

    # ── Invoice 2: NFL, Partially Paid, Globex Inc, $25 000 ─────────────
    inv2 = InvoiceRow(
        id=2,
        invoice_number="INV-2024-00200",
        company_code="NFL",
        status="Partially Paid",
        buyer_business_name="Globex Industries Inc.",
        issue_date=date(2024, 5, 15),
        due_date=date(2024, 6, 15),
        currency="USD",
    )
    session.add(inv2)
    session.flush()
    session.add(
        InvoiceDetailRow(
            id=3, invoice_id=2, product_code="PROD-C",
            quantity=250, unit_price=Decimal("100.00"), sales_value=Decimal("25000.00"),
        ),
    )

    # ── Invoice 3: NFL, Paid (should be excluded) ───────────────────────
    inv3 = InvoiceRow(
        id=3,
        invoice_number="INV-2024-00050",
        company_code="NFL",
        status="Paid",
        buyer_business_name="Wayne Enterprises",
        issue_date=date(2024, 1, 1),
        due_date=date(2024, 2, 1),
        currency="USD",
    )
    session.add(inv3)
    session.flush()
    session.add(
        InvoiceDetailRow(
            id=4, invoice_id=3, product_code="PROD-D",
            quantity=10, unit_price=Decimal("500.00"), sales_value=Decimal("5000.00"),
        ),
    )

    # ── Invoice 4: NBA, Unpaid (different company — should be excluded) ──
    inv4 = InvoiceRow(
        id=4,
        invoice_number="INV-2024-00300",
        company_code="NBA",
        status="Unpaid",
        buyer_business_name="Stark Industries",
        issue_date=date(2024, 6, 10),
        due_date=date(2024, 7, 10),
        currency="USD",
    )
    session.add(inv4)
    session.flush()
    session.add(
        InvoiceDetailRow(
            id=5, invoice_id=4, product_code="PROD-E",
            quantity=20, unit_price=Decimal("750.00"), sales_value=Decimal("15000.00"),
        ),
    )

    # ── Invoice 5: NFL, Unpaid, Acme Corp (different), $50 000 ──────────
    inv5 = InvoiceRow(
        id=5,
        invoice_number="INV-2024-00500",
        company_code="NFL",
        status="Unpaid",
        buyer_business_name="Acme Corporation Holdings",
        issue_date=date(2024, 6, 20),
        due_date=date(2024, 7, 20),
        currency="USD",
    )
    session.add(inv5)
    session.flush()
    session.add(
        InvoiceDetailRow(
            id=6, invoice_id=5, product_code="PROD-F",
            quantity=500, unit_price=Decimal("100.00"), sales_value=Decimal("50000.00"),
        ),
    )

    session.commit()


class _BaseDALTest(unittest.TestCase):
    """Shared setUp/tearDown for all DAL tests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(cls.engine)
        with Session(cls.engine) as session:
            _seed_database(session)

    @classmethod
    def tearDownClass(cls) -> None:
        Base.metadata.drop_all(cls.engine)
        cls.engine.dispose()


# ── Test: fetch_open_invoices ────────────────────────────────────────────────

class TestFetchOpenInvoices(_BaseDALTest):
    """Tests for the open-invoice query (status filtering + JOIN)."""

    def setUp(self) -> None:
        self.dal = InvoiceDAL(self.engine)

    def test_returns_only_nfl_open_invoices(self) -> None:
        """Should return invoices 1, 2, 5 — not 3 (Paid) or 4 (NBA)."""
        results = self.dal.fetch_open_invoices(company_code="NFL")
        ids = {r.invoice_id for r in results}
        self.assertEqual(ids, {1, 2, 5})

    def test_excludes_paid_invoices(self) -> None:
        results = self.dal.fetch_open_invoices(company_code="NFL")
        for r in results:
            self.assertIn(
                r.invoice_number, ["INV-2024-00142", "INV-2024-00200", "INV-2024-00500"]
            )

    def test_excludes_other_company_codes(self) -> None:
        results = self.dal.fetch_open_invoices(company_code="NFL")
        nba_ids = {r.invoice_id for r in results if r.invoice_id == 4}
        self.assertEqual(nba_ids, set())

    def test_invoice_details_are_joined(self) -> None:
        """Each CandidateInvoice should carry its line-item details."""
        results = self.dal.fetch_open_invoices(company_code="NFL")
        inv1 = next(r for r in results if r.invoice_id == 1)
        self.assertEqual(len(inv1.details), 2)
        product_codes = {d.product_code for d in inv1.details}
        self.assertEqual(product_codes, {"PROD-A", "PROD-B"})

    def test_total_sales_value_is_summed(self) -> None:
        results = self.dal.fetch_open_invoices(company_code="NFL")
        inv1 = next(r for r in results if r.invoice_id == 1)
        self.assertEqual(inv1.total_sales_value, Decimal("10000.00"))

    def test_returns_empty_for_unknown_company(self) -> None:
        results = self.dal.fetch_open_invoices(company_code="ZZZZZ")
        self.assertEqual(results, [])

    def test_nba_query_returns_correct_results(self) -> None:
        results = self.dal.fetch_open_invoices(company_code="NBA")
        ids = {r.invoice_id for r in results}
        self.assertEqual(ids, {4})

    def test_result_dto_fields(self) -> None:
        """All DTO fields should be correctly populated."""
        results = self.dal.fetch_open_invoices(company_code="NFL")
        inv2 = next(r for r in results if r.invoice_id == 2)
        self.assertEqual(inv2.invoice_number, "INV-2024-00200")
        self.assertEqual(inv2.buyer_business_name, "Globex Industries Inc.")
        self.assertEqual(inv2.issue_date, date(2024, 5, 15))
        self.assertEqual(inv2.due_date, date(2024, 6, 15))
        self.assertEqual(inv2.currency, "USD")


# ── Test: find_candidates_for_payment ────────────────────────────────────────

class TestFindCandidatesForPayment(_BaseDALTest):
    """Tests for the candidate-search batching function."""

    def setUp(self) -> None:
        self.dal = InvoiceDAL(self.engine)

    def test_amount_variance_match(self) -> None:
        """Payment of $10 500 should match INV 1 ($10 000) within 20 %."""
        candidates = self.dal.find_candidates_for_payment(
            payment_amount=Decimal("10500.00"),
            payment_date=date(2024, 6, 15),
            sender_name="Unknown Sender XYZ",  # no name match
            psid="REF-UNKNOWN",
            company_code="NFL",
        )
        ids = {c.invoice_id for c in candidates}
        self.assertIn(1, ids)  # $10 000 is within 20 % of $10 500

    def test_amount_variance_excludes_far_off(self) -> None:
        """Payment of $10 000 should NOT match INV 5 ($50 000) — 400 % off."""
        candidates = self.dal.find_candidates_for_payment(
            payment_amount=Decimal("10000.00"),
            payment_date=date(2024, 6, 15),
            sender_name="Unknown Sender XYZ",
            psid="REF-UNKNOWN",
            company_code="NFL",
        )
        ids = {c.invoice_id for c in candidates}
        self.assertNotIn(5, ids)

    def test_sender_name_match(self) -> None:
        """Sender 'Acme Corp' should surface INV 1 and INV 5 via LIKE."""
        candidates = self.dal.find_candidates_for_payment(
            payment_amount=Decimal("1.00"),  # amount way off
            payment_date=date(2024, 6, 15),
            sender_name="Acme Corp",
            psid="REF-IRRELEVANT",
            company_code="NFL",
        )
        ids = {c.invoice_id for c in candidates}
        # Both Acme invoices should appear due to entity-name filter
        self.assertIn(1, ids)   # "Acme Corp Ltd."
        self.assertIn(5, ids)   # "Acme Corporation Holdings"

    def test_excludes_paid_invoices(self) -> None:
        """Even if amount matches, Paid invoices must be excluded."""
        candidates = self.dal.find_candidates_for_payment(
            payment_amount=Decimal("5000.00"),
            payment_date=date(2024, 6, 15),
            sender_name="Wayne Enterprises",
            psid="REF-WAYNE",
            company_code="NFL",
        )
        ids = {c.invoice_id for c in candidates}
        self.assertNotIn(3, ids)  # INV 3 is Paid

    def test_excludes_other_company(self) -> None:
        """Only NFL invoices should appear, not NBA."""
        candidates = self.dal.find_candidates_for_payment(
            payment_amount=Decimal("15000.00"),
            payment_date=date(2024, 6, 15),
            sender_name="Stark Industries",
            psid="REF-STARK",
            company_code="NFL",
        )
        ids = {c.invoice_id for c in candidates}
        self.assertNotIn(4, ids)  # INV 4 is NBA

    def test_combined_amount_and_name(self) -> None:
        """Both filters fire — candidates should include union of matches."""
        candidates = self.dal.find_candidates_for_payment(
            payment_amount=Decimal("24000.00"),  # within 20% of 25000
            payment_date=date(2024, 6, 15),
            sender_name="Acme Corp",
            psid="INV-2024-00200",
            company_code="NFL",
        )
        ids = {c.invoice_id for c in candidates}
        # INV 2 matches on amount ($25k ≈ $24k),
        # INV 1 and 5 match on sender name
        self.assertIn(2, ids)
        self.assertIn(1, ids)
        self.assertIn(5, ids)

    def test_no_candidates_found(self) -> None:
        """Wildly off payment returns empty list."""
        candidates = self.dal.find_candidates_for_payment(
            payment_amount=Decimal("999999.99"),
            payment_date=date(2024, 6, 15),
            sender_name="ZZZZZ",
            psid="NOPE",
            company_code="NFL",
        )
        self.assertEqual(candidates, [])

    def test_custom_variance_pct(self) -> None:
        """A tighter 5 % variance should narrow results."""
        # $10 000 ± 5% → $9 500 – $10 500
        candidates = self.dal.find_candidates_for_payment(
            payment_amount=Decimal("10000.00"),
            payment_date=date(2024, 6, 15),
            sender_name="Unknown",
            psid="REF-X",
            company_code="NFL",
            variance_pct=0.05,
        )
        ids = {c.invoice_id for c in candidates}
        self.assertIn(1, ids)       # $10 000 — exact
        self.assertNotIn(2, ids)    # $25 000 — way off
        self.assertNotIn(5, ids)    # $50 000 — way off


# ── Test: match_payment end-to-end ───────────────────────────────────────────

class TestMatchPayment(_BaseDALTest):
    """Integration test: DAL → ReconciliationService → sorted results."""

    def setUp(self) -> None:
        self.dal = InvoiceDAL(self.engine)

    def test_best_match_comes_first(self) -> None:
        """An exact-amount, exact-reference, exact-name payment should rank #1."""
        scored = match_payment(
            self.dal,
            payment_amount=Decimal("10000.00"),
            payment_date=date(2024, 6, 15),
            sender_name="Acme Corp Ltd.",
            psid="INV-2024-00142",
            company_code="NFL",
        )
        self.assertGreater(len(scored), 0)
        best_candidate, best_result = scored[0]
        self.assertEqual(best_candidate.invoice_number, "INV-2024-00142")
        self.assertGreater(best_result.weighted_confidence_index, 0.7)

    def test_results_are_sorted_descending(self) -> None:
        scored = match_payment(
            self.dal,
            payment_amount=Decimal("10000.00"),
            payment_date=date(2024, 6, 15),
            sender_name="Acme Corp",
            psid="INV-2024-00142",
            company_code="NFL",
        )
        wcis = [r.weighted_confidence_index for _, r in scored]
        self.assertEqual(wcis, sorted(wcis, reverse=True))

    def test_all_results_have_valid_scores(self) -> None:
        scored = match_payment(
            self.dal,
            payment_amount=Decimal("10000.00"),
            payment_date=date(2024, 6, 15),
            sender_name="Acme Corp",
            psid="INV-2024-00142",
            company_code="NFL",
        )
        for _, result in scored:
            self.assertGreaterEqual(result.weighted_confidence_index, 0.0)
            self.assertLessEqual(result.weighted_confidence_index, 1.0)


# ── Test: DTO structure ──────────────────────────────────────────────────────

class TestDTOStructure(unittest.TestCase):
    """Tests for the lightweight DTO dataclasses."""

    def test_candidate_invoice_is_frozen(self) -> None:
        dto = CandidateInvoice(
            invoice_id=1,
            invoice_number="INV-001",
            buyer_business_name="Test Co",
            buyer_iden_code="TEST01",
            issue_date=date(2024, 1, 1),
            due_date=date(2024, 2, 1),
            currency="USD",
            total_sales_value=Decimal("1000"),
        )
        with self.assertRaises(AttributeError):
            dto.invoice_id = 99  # type: ignore[misc]

    def test_detail_dto_is_frozen(self) -> None:
        dto = InvoiceDetailDTO(
            product_code="P1",
            quantity=5,
            unit_price=Decimal("10.00"),
            sales_value=Decimal("50.00"),
        )
        with self.assertRaises(AttributeError):
            dto.quantity = 99  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
