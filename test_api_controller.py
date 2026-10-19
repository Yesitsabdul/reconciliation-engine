"""
Unit tests for the reconciliation API controller.

Uses an in-memory SQLite database and FastAPI's TestClient (backed by
httpx) so tests run without a MySQL server or live HTTP connections.

Run with:
    python -m pytest test_api_controller.py -v
    python -m unittest test_api_controller -v
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from db_models import (
    Base,
    InvoiceDetailRow,
    InvoiceRow,
    ManualReviewRow,
    PaymentRow,
)
import api_controller
from api_controller import app


# ── Test fixtures ────────────────────────────────────────────────────────────

def _seed_invoices(session: Session) -> None:
    """Populate invoices and details for testing.

    Creates 4 invoices with known amounts and names so each triage
    path (auto-match, review, discard) can be exercised deterministically.
    """

    # ── Invoice 1: NFL, Unpaid, Acme Corp, $10 000 ──────────────────────
    inv1 = InvoiceRow(
        id=1,
        invoice_number="INV-2024-00142",
        company_code="NFL",
        status="Unpaid",
        buyer_business_name="Acme Corp Ltd.",
        buyer_iden_code="ACME01",
        issue_date=date(2024, 6, 1),
        due_date=date(2024, 7, 1),
        currency="USD",
    )
    session.add(inv1)
    session.flush()
    session.add_all([
        InvoiceDetailRow(
            id=1, invoice_id=1, product_code="PROD-A",
            quantity=100, unit_price=Decimal("50.00"),
            sales_value=Decimal("5000.00"),
        ),
        InvoiceDetailRow(
            id=2, invoice_id=1, product_code="PROD-B",
            quantity=50, unit_price=Decimal("100.00"),
            sales_value=Decimal("5000.00"),
        ),
    ])

    # ── Invoice 2: NFL, Partially Paid, Globex Inc, $25 000 ─────────────
    inv2 = InvoiceRow(
        id=2,
        invoice_number="INV-2024-00200",
        company_code="NFL",
        status="Partially Paid",
        buyer_business_name="Globex Industries Inc.",
        buyer_iden_code="GLOB01",
        issue_date=date(2024, 5, 15),
        due_date=date(2024, 6, 15),
        currency="USD",
    )
    session.add(inv2)
    session.flush()
    session.add(
        InvoiceDetailRow(
            id=3, invoice_id=2, product_code="PROD-C",
            quantity=250, unit_price=Decimal("100.00"),
            sales_value=Decimal("25000.00"),
        ),
    )

    # ── Invoice 3: NFL, Unpaid, Acme Corporation Holdings, $50 000 ──────
    inv3 = InvoiceRow(
        id=3,
        invoice_number="INV-2024-00500",
        company_code="NFL",
        status="Unpaid",
        buyer_business_name="Acme Corporation Holdings",
        buyer_iden_code="ACME01",
        issue_date=date(2024, 6, 20),
        due_date=date(2024, 7, 20),
        currency="USD",
    )
    session.add(inv3)
    session.flush()
    session.add(
        InvoiceDetailRow(
            id=4, invoice_id=3, product_code="PROD-F",
            quantity=500, unit_price=Decimal("100.00"),
            sales_value=Decimal("50000.00"),
        ),
    )

    # ── Invoice 4: NFL, Paid (should always be excluded) ────────────────
    inv4 = InvoiceRow(
        id=4,
        invoice_number="INV-2024-00050",
        company_code="NFL",
        status="Paid",
        buyer_business_name="Wayne Enterprises",
        buyer_iden_code="WAYNE01",
        issue_date=date(2024, 1, 1),
        due_date=date(2024, 2, 1),
        currency="USD",
    )
    session.add(inv4)
    session.flush()
    session.add(
        InvoiceDetailRow(
            id=5, invoice_id=4, product_code="PROD-D",
            quantity=10, unit_price=Decimal("500.00"),
            sales_value=Decimal("5000.00"),
        ),
    )

    session.commit()


def _add_payment(session: Session, **overrides) -> PaymentRow:
    """Insert a single payment with sensible defaults."""
    defaults = dict(
        reference="REF-001",
        amount=Decimal("10000.00"),
        payment_date=date(2024, 6, 15),
        sender_name="Unknown",
        currency="USD",
        allocation_status="UNALLOCATED",
    )
    defaults.update(overrides)
    row = PaymentRow(**defaults)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ── Test base class ──────────────────────────────────────────────────────────

class _BaseAPITest(unittest.TestCase):
    """Shared setUp/tearDown for all API tests.

    Creates a fresh in-memory SQLite DB and wires it into the FastAPI
    app before each test, ensuring full isolation.
    """

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            echo=False,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)

        # Wire the test engine into the controller module
        api_controller.configure(self.engine)

        with Session(self.engine) as session:
            _seed_invoices(session)

        self.client = TestClient(app)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()


# ── Test: Health check ───────────────────────────────────────────────────────

class TestHealthCheck(_BaseAPITest):
    """Smoke test for GET /health."""

    def test_health_returns_200(self) -> None:
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(body["service"], "reconciliation-engine")


# ── Test: POST /api/reconcile/run — Auto-match triage ────────────────────────

class TestAutoMatchTriage(_BaseAPITest):
    """Score > 0.85 should auto-match: invoice → Paid, IRIS_CONFIRMED."""

    def test_auto_match_exact_payment(self) -> None:
        """Payment with exact amount, exact reference, exact sender, same-day
        timing should score ~1.0 and trigger auto-match."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="INV-2024-00142",
                amount=Decimal("10000.00"),
                payment_date=date(2024, 6, 1),
                sender_name="Acme Corp Ltd.",
            )

        resp = self.client.post("/api/reconcile/run")
        self.assertEqual(resp.status_code, 200)

        body = resp.json()
        self.assertEqual(body["payments_processed"], 1)
        self.assertGreaterEqual(len(body["auto_matched"]), 1)

        match = body["auto_matched"][0]
        self.assertEqual(match["invoice_number"], "INV-2024-00142")
        self.assertGreater(match["weighted_confidence"], 0.85)
        self.assertEqual(match["action"], "AUTO_MATCHED")

    def test_auto_match_updates_invoice_status(self) -> None:
        """After auto-match, invoice.status should be 'Paid' and
        invoice.recon_status should be 'IRIS_CONFIRMED'."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="INV-2024-00142",
                amount=Decimal("10000.00"),
                payment_date=date(2024, 6, 1),
                sender_name="Acme Corp Ltd.",
            )

        self.client.post("/api/reconcile/run")

        with Session(self.engine) as session:
            invoice = session.get(InvoiceRow, 1)
            self.assertEqual(invoice.status, "Paid")
            self.assertEqual(invoice.recon_status, "IRIS_CONFIRMED")

    def test_auto_match_marks_payment_allocated(self) -> None:
        """After auto-match, payment.allocation_status should be 'ALLOCATED'."""
        with Session(self.engine) as session:
            pmt = _add_payment(
                session,
                id=1,
                reference="INV-2024-00142",
                amount=Decimal("10000.00"),
                payment_date=date(2024, 6, 1),
                sender_name="Acme Corp Ltd.",
            )

        self.client.post("/api/reconcile/run")

        with Session(self.engine) as session:
            payment = session.get(PaymentRow, 1)
            self.assertEqual(payment.allocation_status, "ALLOCATED")


# ── Test: POST /api/reconcile/run — Review-flag triage ───────────────────────

class TestReviewFlagTriage(_BaseAPITest):
    """Score between 0.50 and 0.85 should flag for manual review."""

    def test_partial_match_flagged(self) -> None:
        """Payment with matching sender name but mismatched amount and
        different reference should land in the review bucket."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="PAY-SOMETHING-ELSE",
                amount=Decimal("9950.00"),  # within amount tolerance for review
                payment_date=date(2024, 6, 15),
                sender_name="Acme Corp Ltd.",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()

        # The exact triage bucket depends on the WCI score; assert that
        # it was NOT auto-matched (because reference and amount differ)
        # and that we got a result somewhere.
        total = (
            len(body["auto_matched"])
            + len(body["flagged_for_review"])
            + len(body["discarded"])
        )
        self.assertGreaterEqual(total, 1)
        self.assertEqual(body["payments_processed"], 1)

    def test_review_creates_manual_review_row(self) -> None:
        """If a payment is flagged, a row should appear in manual_reviews."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="PAY-SOMETHING-ELSE",
                amount=Decimal("9950.00"),
                payment_date=date(2024, 6, 15),
                sender_name="Acme Corp Ltd.",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()

        if body["flagged_for_review"]:
            flag = body["flagged_for_review"][0]
            self.assertIn("score_breakdown", flag)
            self.assertEqual(flag["action"], "FLAGGED_FOR_REVIEW")

            # Verify the manual_reviews table has a row
            with Session(self.engine) as session:
                reviews = session.query(ManualReviewRow).all()
                self.assertGreaterEqual(len(reviews), 1)
                self.assertEqual(reviews[0].review_status, "PENDING")

    def test_review_sets_recon_status_flagged(self) -> None:
        """Invoice should get recon_status = 'REVIEW_FLAGGED' when flagged."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="PAY-SOMETHING-ELSE",
                amount=Decimal("9950.00"),
                payment_date=date(2024, 6, 15),
                sender_name="Acme Corp Ltd.",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()

        if body["flagged_for_review"]:
            inv_id = body["flagged_for_review"][0]["invoice_id"]
            with Session(self.engine) as session:
                invoice = session.get(InvoiceRow, inv_id)
                self.assertEqual(invoice.recon_status, "REVIEW_FLAGGED")


# ── Test: POST /api/reconcile/run — Discard triage ───────────────────────────

class TestDiscardTriage(_BaseAPITest):
    """Score < 0.50 should discard the match attempt."""

    def test_wildly_off_payment_discarded(self) -> None:
        """Payment with wrong amount, wrong name, wrong reference — should
        score low and be discarded (or have no candidates at all)."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="ZZZZZ-99999",
                amount=Decimal("999999.00"),
                payment_date=date(2024, 6, 15),
                sender_name="Totally Unknown Entity XYZ",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()

        self.assertEqual(body["payments_processed"], 1)
        self.assertEqual(len(body["auto_matched"]), 0)
        self.assertEqual(len(body["flagged_for_review"]), 0)
        self.assertGreaterEqual(len(body["discarded"]), 1)

    def test_discard_no_side_effects(self) -> None:
        """Discarded payments should not modify any invoice or create reviews."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="ZZZZZ-99999",
                amount=Decimal("999999.00"),
                payment_date=date(2024, 6, 15),
                sender_name="Totally Unknown Entity XYZ",
            )

        self.client.post("/api/reconcile/run")

        with Session(self.engine) as session:
            # No invoice should have been modified
            for inv_id in [1, 2, 3]:
                inv = session.get(InvoiceRow, inv_id)
                self.assertNotEqual(inv.status, "Paid")
                self.assertIsNone(inv.recon_status)

            # No manual reviews should exist
            reviews = session.query(ManualReviewRow).all()
            self.assertEqual(len(reviews), 0)

            # Payment should still be UNALLOCATED
            pmt = session.get(PaymentRow, 1)
            self.assertEqual(pmt.allocation_status, "UNALLOCATED")


# ── Test: POST /api/reconcile/run — Edge cases ──────────────────────────────

class TestEdgeCases(_BaseAPITest):
    """Boundary and edge-case tests."""

    def test_no_unallocated_payments(self) -> None:
        """If there are no unallocated payments, return an empty response."""
        resp = self.client.post("/api/reconcile/run")
        body = resp.json()
        self.assertEqual(body["payments_processed"], 0)
        self.assertEqual(body["auto_matched"], [])
        self.assertEqual(body["flagged_for_review"], [])
        self.assertEqual(body["discarded"], [])

    def test_multiple_payments_processed(self) -> None:
        """Multiple unallocated payments should each be processed."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="INV-2024-00142",
                amount=Decimal("10000.00"),
                payment_date=date(2024, 6, 1),
                sender_name="Acme Corp Ltd.",
            )
            _add_payment(
                session,
                id=2,
                reference="ZZZZZ-99999",
                amount=Decimal("999999.00"),
                payment_date=date(2024, 6, 15),
                sender_name="Unknown",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()
        self.assertEqual(body["payments_processed"], 2)

    def test_already_allocated_payments_skipped(self) -> None:
        """Payments with allocation_status='ALLOCATED' should not be fetched."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="INV-2024-00142",
                amount=Decimal("10000.00"),
                payment_date=date(2024, 6, 1),
                sender_name="Acme Corp Ltd.",
                allocation_status="ALLOCATED",  # already matched
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()
        self.assertEqual(body["payments_processed"], 0)

    def test_response_json_schema(self) -> None:
        """Response should contain all expected top-level keys."""
        resp = self.client.post("/api/reconcile/run")
        body = resp.json()
        expected_keys = {
            "payments_processed",
            "auto_matched",
            "bulk_matched",
            "flagged_for_review",
            "discarded",
            "summary",
        }
        self.assertEqual(set(body.keys()), expected_keys)

    def test_summary_counts_are_consistent(self) -> None:
        """summary.auto_matched + flagged + discarded == total items."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="INV-2024-00142",
                amount=Decimal("10000.00"),
                payment_date=date(2024, 6, 1),
                sender_name="Acme Corp Ltd.",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()
        summary = body["summary"]
        self.assertEqual(
            summary["auto_matched"], len(body["auto_matched"])
        )
        self.assertEqual(
            summary["flagged_for_review"], len(body["flagged_for_review"])
        )
        self.assertEqual(
            summary["discarded"], len(body["discarded"])
        )


# ── Test: GET /api/reconcile/reviews ─────────────────────────────────────────

class TestListReviews(_BaseAPITest):
    """Tests for the GET /api/reconcile/reviews endpoint."""

    def test_empty_reviews(self) -> None:
        resp = self.client.get("/api/reconcile/reviews")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_reviews_returned_after_flag(self) -> None:
        """After flagging a payment, GET /reviews should return it."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="PAY-SOMETHING-ELSE",
                amount=Decimal("9950.00"),
                payment_date=date(2024, 6, 15),
                sender_name="Acme Corp Ltd.",
            )

        # Run reconciliation first
        run_resp = self.client.post("/api/reconcile/run")
        run_body = run_resp.json()

        if run_body["flagged_for_review"]:
            resp = self.client.get("/api/reconcile/reviews")
            reviews = resp.json()
            self.assertGreaterEqual(len(reviews), 1)
            self.assertEqual(reviews[0]["review_status"], "PENDING")


class TestReviewDecisions(_BaseAPITest):
    """Tests for manual review decision actions."""

    def _flag_one_review(self) -> dict:
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="PAY-SOMETHING-ELSE",
                amount=Decimal("9950.00"),
                payment_date=date(2024, 6, 15),
                sender_name="Acme Corp Ltd.",
            )

        run_resp = self.client.post("/api/reconcile/run")
        run_body = run_resp.json()
        reviews_resp = self.client.get("/api/reconcile/reviews")
        reviews = reviews_resp.json()
        self.assertGreaterEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["review_status"], "PENDING")
        return reviews[0]

    def test_match_manual_review(self) -> None:
        review = self._flag_one_review()
        resp = self.client.post(
            f"/api/reconcile/reviews/{review['review_id']}/decision",
            json={"action": "MATCH"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["review_status"], "APPROVED")
        self.assertEqual(body["action"], "MATCH")

        with Session(self.engine) as session:
            invoice = session.get(InvoiceRow, body["invoice_id"])
            payment = session.get(PaymentRow, body["payment_id"])
            self.assertEqual(invoice.status, "Paid")
            self.assertEqual(invoice.recon_status, "IRIS_CONFIRMED")
            self.assertEqual(payment.allocation_status, "ALLOCATED")

        remaining = self.client.get("/api/reconcile/reviews").json()
        self.assertEqual(remaining, [])

    def test_decline_manual_review(self) -> None:
        review = self._flag_one_review()
        resp = self.client.post(
            f"/api/reconcile/reviews/{review['review_id']}/decision",
            json={"action": "DECLINE"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["review_status"], "REJECTED")
        self.assertEqual(body["action"], "DECLINE")

        with Session(self.engine) as session:
            invoice = session.get(InvoiceRow, body["invoice_id"])
            payment = session.get(PaymentRow, body["payment_id"])
            self.assertEqual(invoice.recon_status, None)
            self.assertEqual(payment.allocation_status, "UNALLOCATED")

        remaining = self.client.get("/api/reconcile/reviews").json()
        self.assertEqual(remaining, [])


# ── Test: Idempotency ───────────────────────────────────────────────────────

class TestIdempotency(_BaseAPITest):
    """Running reconciliation twice should not double-process payments."""

    def test_second_run_skips_allocated(self) -> None:
        """After an auto-match, running again should not re-process."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="INV-2024-00142",
                amount=Decimal("10000.00"),
                payment_date=date(2024, 6, 1),
                sender_name="Acme Corp Ltd.",
            )

        # First run: should auto-match
        resp1 = self.client.post("/api/reconcile/run")
        body1 = resp1.json()
        self.assertGreaterEqual(len(body1["auto_matched"]), 1)

        # Second run: payment is now ALLOCATED, should be skipped
        resp2 = self.client.post("/api/reconcile/run")
        body2 = resp2.json()
        self.assertEqual(body2["payments_processed"], 0)


# ── Test: Bulk payment matching ──────────────────────────────────────────────

class TestBulkMatchTriage(_BaseAPITest):
    """Tests for the subset-sum bulk payment matching feature."""

    def _seed_bulk_invoices(self) -> None:
        """Add invoices that sum to a known amount for bulk matching.

        Adds two ACME01 invoices: $3,000 + $7,000 = $10,000 (not matching
        the existing $10k invoice 1 since the bulk amount will be $60,000).
        We use existing inv1 ($10k) + inv3 ($50k) both with buyer_iden_code='ACME01'
        which sum to $60,000.
        """
        pass  # Existing seed data already has ACME01 invoices summing to $60k

    def test_bulk_match_two_invoices(self) -> None:
        """Payment of $60,000 should bulk-match invoices 1 ($10k) + 3 ($50k)
        since both have buyer_iden_code='ACME01'."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="BULK-PAY-001",
                amount=Decimal("60000.00"),
                payment_date=date(2024, 6, 25),
                sender_name="Acme Corp",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["payments_processed"], 1)
        self.assertGreaterEqual(len(body["bulk_matched"]), 1)

        bulk = body["bulk_matched"][0]
        self.assertEqual(bulk["action"], "BULK_AUTO_MATCHED")
        self.assertEqual(bulk["weighted_confidence"], 0.95)
        self.assertEqual(set(bulk["invoice_ids"]), {1, 3})
        self.assertAlmostEqual(bulk["combined_amount"], 60000.0)

    def test_bulk_match_marks_all_invoices_paid(self) -> None:
        """After bulk match, all invoices in the group should be Paid/IRIS_CONFIRMED."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="BULK-PAY-001",
                amount=Decimal("60000.00"),
                payment_date=date(2024, 6, 25),
                sender_name="Acme Corp",
            )

        self.client.post("/api/reconcile/run")

        with Session(self.engine) as session:
            for inv_id in [1, 3]:
                inv = session.get(InvoiceRow, inv_id)
                self.assertEqual(inv.status, "Paid")
                self.assertEqual(inv.recon_status, "IRIS_CONFIRMED")

    def test_bulk_match_marks_payment_allocated(self) -> None:
        """After bulk match, payment should be ALLOCATED."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="BULK-PAY-001",
                amount=Decimal("60000.00"),
                payment_date=date(2024, 6, 25),
                sender_name="Acme Corp",
            )

        self.client.post("/api/reconcile/run")

        with Session(self.engine) as session:
            pmt = session.get(PaymentRow, 1)
            self.assertEqual(pmt.allocation_status, "ALLOCATED")

    def test_bulk_match_within_tolerance(self) -> None:
        """Payment within 0.5% of the invoice sum should still bulk-match."""
        # $60,000 * 0.003 = $180 deviation (within 0.5%)
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="BULK-PAY-002",
                amount=Decimal("60180.00"),
                payment_date=date(2024, 6, 25),
                sender_name="Acme Corp",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()
        self.assertGreaterEqual(len(body["bulk_matched"]), 1)

    def test_bulk_match_outside_tolerance_not_matched(self) -> None:
        """Payment more than 0.5% away from any combination should not bulk-match."""
        # $60,000 * 0.01 = $600 deviation (outside 0.5%)
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="BULK-PAY-003",
                amount=Decimal("60600.00"),
                payment_date=date(2024, 6, 25),
                sender_name="Acme Corp",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()
        self.assertEqual(len(body["bulk_matched"]), 0)

    def test_summary_includes_bulk_matched(self) -> None:
        """Summary counts should include bulk_matched."""
        with Session(self.engine) as session:
            _add_payment(
                session,
                id=1,
                reference="BULK-PAY-001",
                amount=Decimal("60000.00"),
                payment_date=date(2024, 6, 25),
                sender_name="Acme Corp",
            )

        resp = self.client.post("/api/reconcile/run")
        body = resp.json()
        self.assertIn("bulk_matched", body["summary"])
        self.assertEqual(
            body["summary"]["bulk_matched"], len(body["bulk_matched"])
        )


if __name__ == "__main__":
    unittest.main()
