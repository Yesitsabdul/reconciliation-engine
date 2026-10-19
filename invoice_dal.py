"""
Data Access Layer (DAL) for the invoice-to-payment reconciliation engine.

Provides two main capabilities:

1. **Fetch open invoices** — retrieves all Unpaid / Partially Paid invoices
   for a given company code, joined with ``invoice_details`` for product-level
   data.  The query is structured to hit the composite index
   ``idx_invoices_company_code_status(company_code, status)``.

2. **Candidate invoice search** — given an incoming bank payment, efficiently
   narrows the full invoice table down to plausible matches using:
     • 20 % amount-variance filter  (server-side, index-friendly)
     • Loose sender-name match      (server-side LIKE / SOUNDEX)

The module is intentionally ORM-agnostic on the *public surface*:
callers receive plain ``CandidateInvoice`` dataclass instances so
the reconciliation engine never couples to SQLAlchemy.

Usage::

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine("mysql+pymysql://user:pass@host/db")
    dal    = InvoiceDAL(engine)

    # 1) All open NFL invoices
    open_invoices = dal.fetch_open_invoices(company_code="NFL")

    # 2) Candidate invoices for a payment
    candidates = dal.find_candidates_for_payment(
        payment_amount=Decimal("12500.00"),
        payment_date=date(2024, 7, 10),
        sender_name="Acme Corp",
        psid="INV-2024-00142",
        company_code="NFL",
    )
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import Engine, Select, and_, func, or_, select, text
from sqlalchemy.orm import Session, joinedload

from db_models import Base, InvoiceDetailRow, InvoiceRow, ManualReviewRow, PaymentRow

logger = logging.getLogger(__name__)

# ── Statuses considered "open" for matching ──────────────────────────────────

OPEN_STATUSES: frozenset[str] = frozenset({"Unpaid", "Partially Paid"})


# ── Lightweight result DTOs ──────────────────────────────────────────────────

@dataclass(frozen=True)
class InvoiceDetailDTO:
    """Flat projection of a single invoice-detail line."""
    product_code: str
    quantity: int
    unit_price: Decimal
    sales_value: Decimal


@dataclass(frozen=True)
class CandidateInvoice:
    """Flat projection of an invoice + aggregated detail data.

    This is what the reconciliation engine receives — completely decoupled
    from the ORM layer.
    """
    invoice_id: int
    invoice_number: str
    buyer_business_name: str
    buyer_iden_code: str
    issue_date: date
    due_date: date
    currency: str
    total_sales_value: Decimal
    details: list[InvoiceDetailDTO] = field(default_factory=list)


@dataclass(frozen=True)
class PaymentDTO:
    """Flat projection of a bank payment row."""
    payment_id: int
    reference: str
    amount: Decimal
    payment_date: date
    sender_name: str
    currency: str
    allocation_status: str


# ── Core queries (pure SQL expressions) ─────────────────────────────────────

def _build_open_invoices_query(company_code: str) -> Select:
    """Build a SELECT for open invoices joined with details.

    The WHERE clause is ordered so the MySQL optimizer can do an
    index-range scan on ``idx_invoices_company_code_status``:

        WHERE company_code = :code AND status IN ('Unpaid', 'Partially Paid')

    Because ``company_code`` is the *leftmost* column of the composite
    index, the engine can seek directly into the right partition, then
    scan only the qualifying ``status`` values.
    """
    return (
        select(InvoiceRow)
        .where(
            and_(
                InvoiceRow.company_code == company_code,
                InvoiceRow.status.in_(OPEN_STATUSES),
            )
        )
        .options(joinedload(InvoiceRow.details))   # single JOIN, no N+1
        .order_by(InvoiceRow.id)
    )


def _build_candidate_query(
    *,
    payment_amount: Decimal,
    sender_name: str,
    company_code: str,
    variance_pct: float = 0.20,
) -> Select:
    """Build a SELECT for candidate invoices that could match a payment.

    Filtering strategy
    ──────────────────
    The query applies an **OR** of two server-side filters so that MySQL
    need only touch rows that satisfy *at least one* criterion:

    1. **Amount variance** — ``SUM(invoice_details.sales_value)`` is
       within ± ``variance_pct`` (default 20 %) of the payment amount.
       This is evaluated via a ``HAVING`` clause on the grouped join.

    2. **Sender-name match** — the ``buyer_business_name`` column
       contains any significant token from the sender's name
       (``LIKE '%token%'``).  This catches partial / abbreviated names
       that Levenshtein will later score precisely.

    Both branches still require
    ``company_code = :code AND status IN ('Unpaid', 'Partially Paid')``
    so the composite index is always used for the initial seek.

    Parameters:
        payment_amount: The amount received from the bank.
        sender_name: The payer's name as reported by the bank.
        company_code: Company partition to search.
        variance_pct: Fraction of the payment amount used as the ± window
                      (default 0.20 = 20 %).

    Returns:
        A SQLAlchemy ``Select`` statement.
    """
    lower_bound = payment_amount * Decimal(1 - variance_pct)
    upper_bound = payment_amount * Decimal(1 + variance_pct)

    # Extract meaningful tokens from sender name for LIKE matching.
    # Drop very short tokens (≤2 chars) to avoid noisy matches.
    tokens = [t for t in sender_name.strip().split() if len(t) > 2]
    name_predicates = [
        func.lower(InvoiceRow.buyer_business_name).like(f"%{tok.lower()}%")
        for tok in tokens
    ]

    # Aggregate sales_value per invoice
    total_sales = func.coalesce(
        func.sum(InvoiceDetailRow.sales_value),
        Decimal("0"),
    ).label("total_sales_value")

    stmt = (
        select(
            InvoiceRow.id,
            InvoiceRow.invoice_number,
            InvoiceRow.buyer_business_name,
            InvoiceRow.issue_date,
            InvoiceRow.due_date,
            InvoiceRow.currency,
            total_sales,
        )
        .join(InvoiceDetailRow, InvoiceDetailRow.invoice_id == InvoiceRow.id)
        .where(
            and_(
                InvoiceRow.company_code == company_code,
                InvoiceRow.status.in_(OPEN_STATUSES),
            )
        )
        .group_by(
            InvoiceRow.id,
            InvoiceRow.invoice_number,
            InvoiceRow.buyer_business_name,
            InvoiceRow.issue_date,
            InvoiceRow.due_date,
            InvoiceRow.currency,
        )
    )

    # Build the HAVING / WHERE OR clause for the two candidate criteria.
    amount_filter = total_sales.between(lower_bound, upper_bound)

    if name_predicates:
        # At least one sender-name token must appear in buyer_business_name
        # OR the amount must be within the variance window.
        name_filter = or_(*name_predicates)
        stmt = stmt.having(or_(amount_filter, name_filter))
    else:
        # No usable name tokens — rely on amount filter only.
        stmt = stmt.having(amount_filter)

    stmt = stmt.order_by(InvoiceRow.id)
    return stmt


# ── Data Access Layer ────────────────────────────────────────────────────────

class InvoiceDAL:
    """Data access layer for invoice queries.

    Encapsulates session management and converts ORM rows into lightweight
    DTOs that the reconciliation service can consume without importing
    SQLAlchemy.

    Args:
        engine: A SQLAlchemy ``Engine`` connected to the target database.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ── 1) Fetch open invoices ───────────────────────────────────────────

    def fetch_open_invoices(
        self,
        company_code: str = "NFL",
        *,
        batch_size: int = 500,
    ) -> list[CandidateInvoice]:
        """Fetch all open invoices for a company, joined with line-item details.

        The query leverages ``idx_invoices_company_code_status`` for an
        efficient index-range scan, and uses ``joinedload`` to fetch all
        ``invoice_details`` rows in a single SQL round-trip.

        Args:
            company_code: Company code to filter on (default ``"NFL"``).
            batch_size: Number of invoice rows to process per chunk when
                        streaming from the result set.  Does *not* limit
                        the total result count.

        Returns:
            A list of ``CandidateInvoice`` DTOs.
        """
        stmt = _build_open_invoices_query(company_code)

        results: list[CandidateInvoice] = []
        with Session(self._engine) as session:
            rows: Sequence[InvoiceRow] = (
                session.execute(stmt).scalars().unique().all()
            )
            for row in rows:
                total_sales = sum(
                    (d.sales_value for d in row.details), Decimal("0")
                )
                details = [
                    InvoiceDetailDTO(
                        product_code=d.product_code,
                        quantity=d.quantity,
                        unit_price=d.unit_price,
                        sales_value=d.sales_value,
                    )
                    for d in row.details
                ]
                results.append(
                    CandidateInvoice(
                        invoice_id=row.id,
                        invoice_number=row.invoice_number,
                        buyer_business_name=row.buyer_business_name,
                        buyer_iden_code=row.buyer_iden_code or "",
                        issue_date=row.issue_date,
                        due_date=row.due_date,
                        currency=row.currency,
                        total_sales_value=total_sales,
                        details=details,
                    )
                )

        logger.info(
            "fetch_open_invoices(company_code=%r) → %d invoice(s)",
            company_code,
            len(results),
        )
        return results

    # ── 2) Candidate search for a payment ────────────────────────────────

    def find_candidates_for_payment(
        self,
        *,
        payment_amount: Decimal,
        payment_date: date,
        sender_name: str,
        psid: str,
        company_code: str = "NFL",
        variance_pct: float = 0.20,
    ) -> list[CandidateInvoice]:
        """Query the database for invoices that could match an incoming payment.

        Two OR-ed filters are applied server-side so the DB only returns
        plausible candidates:

        1. **Amount filter** — ``total_sales_value`` is within
           ±``variance_pct`` (default 20 %) of ``payment_amount``.
        2. **Entity filter** — ``buyer_business_name`` loosely matches
           the ``sender_name`` (token-based ``LIKE``).

        The remaining precision (exact Levenshtein scoring, timing decay,
        reference matching) is handled by ``ReconciliationService`` on
        the application side.

        Args:
            payment_amount: The payment amount received.
            payment_date: The date the payment was received.
            sender_name: Payer name from the bank statement.
            psid: Payment Services ID / reference string.
            company_code: Company partition (default ``"NFL"``).
            variance_pct: Fractional tolerance for the amount window.

        Returns:
            A list of ``CandidateInvoice`` DTOs, ready for scoring.
        """
        stmt = _build_candidate_query(
            payment_amount=payment_amount,
            sender_name=sender_name,
            company_code=company_code,
            variance_pct=variance_pct,
        )

        results: list[CandidateInvoice] = []
        with Session(self._engine) as session:
            rows = session.execute(stmt).all()
            for row in rows:
                results.append(
                    CandidateInvoice(
                        invoice_id=row.id,
                        invoice_number=row.invoice_number,
                        buyer_business_name=row.buyer_business_name,
                        buyer_iden_code=getattr(row, 'buyer_iden_code', '') or "",
                        issue_date=row.issue_date,
                        due_date=row.due_date,
                        currency=row.currency,
                        total_sales_value=row.total_sales_value or Decimal("0"),
                    )
                )

        logger.info(
            "find_candidates_for_payment(amount=%s, sender=%r) → %d candidate(s)",
            payment_amount,
            sender_name,
            len(results),
        )
        return results

    # ── 3) Fetch unallocated payments ──────────────────────────────────────

    def fetch_unallocated_payments(self) -> list[PaymentDTO]:
        """Fetch all payments that have not yet been matched to an invoice.

        Returns:
            A list of ``PaymentDTO`` instances with
            ``allocation_status == 'UNALLOCATED'``.
        """
        stmt = (
            select(PaymentRow)
            .where(PaymentRow.allocation_status == "UNALLOCATED")
            .order_by(PaymentRow.id)
        )

        results: list[PaymentDTO] = []
        with Session(self._engine) as session:
            rows: Sequence[PaymentRow] = session.execute(stmt).scalars().all()
            for row in rows:
                results.append(
                    PaymentDTO(
                        payment_id=row.id,
                        reference=row.reference,
                        amount=row.amount,
                        payment_date=row.payment_date,
                        sender_name=row.sender_name,
                        currency=row.currency,
                        allocation_status=row.allocation_status,
                    )
                )

        logger.info(
            "fetch_unallocated_payments() → %d payment(s)", len(results)
        )
        return results

    # ── 4) Auto-match: mark invoice paid + payment allocated ────────────

    def auto_match_invoice(
        self,
        *,
        invoice_id: int,
        payment_id: int,
    ) -> None:
        """Mark an invoice as paid and its payment as allocated.

        Both updates happen inside a single transaction so that the
        system never ends up in an inconsistent state.

        Args:
            invoice_id: PK of the invoice to mark as paid.
            payment_id: PK of the payment to mark as allocated.
        """
        with Session(self._engine) as session:
            with session.begin():
                invoice = session.get(InvoiceRow, invoice_id)
                if invoice is None:
                    raise ValueError(f"Invoice {invoice_id} not found.")
                invoice.status = "Paid"
                invoice.recon_status = "IRIS_CONFIRMED"

                payment = session.get(PaymentRow, payment_id)
                if payment is None:
                    raise ValueError(f"Payment {payment_id} not found.")
                payment.allocation_status = "ALLOCATED"

        logger.info(
            "auto_match_invoice(invoice=%d, payment=%d) → IRIS_CONFIRMED",
            invoice_id,
            payment_id,
        )

    # ── 5) Save a flagged pair for manual review ───────────────────────

    def save_manual_review(
        self,
        *,
        invoice_id: int,
        payment_id: int,
        result: "ReconciliationResult",
    ) -> int:
        """Persist a match pair that needs human review.

        Inserts a row into ``manual_reviews`` with the full score
        breakdown and sets the invoice's ``recon_status`` to
        ``REVIEW_FLAGGED``.

        Args:
            invoice_id: PK of the candidate invoice.
            payment_id: PK of the incoming payment.
            result: The ``ReconciliationResult`` from the scoring engine.

        Returns:
            The auto-generated PK of the new ``manual_reviews`` row.
        """
        from models import ReconciliationResult  # deferred to avoid circular

        with Session(self._engine) as session:
            with session.begin():
                review = ManualReviewRow(
                    invoice_id=invoice_id,
                    payment_id=payment_id,
                    amount_score=Decimal(str(result.amount_score)),
                    reference_score=Decimal(str(result.reference_score)),
                    timing_score=Decimal(str(result.timing_score)),
                    entity_score=Decimal(str(result.entity_score)),
                    weighted_confidence=Decimal(
                        str(result.weighted_confidence_index)
                    ),
                    score_details=json.dumps(result.details),
                    review_status="PENDING",
                )

                if self._engine.dialect.name == "sqlite":
                    max_id = session.scalar(select(func.max(ManualReviewRow.id)))
                    review.id = (max_id or 0) + 1

                session.add(review)

                invoice = session.get(InvoiceRow, invoice_id)
                if invoice is not None:
                    invoice.recon_status = "REVIEW_FLAGGED"

                session.flush()
                review_id = review.id

        logger.info(
            "save_manual_review(invoice=%d, payment=%d, wci=%.4f) → review #%d",
            invoice_id,
            payment_id,
            result.weighted_confidence_index,
            review_id,
        )
        return review_id

    # ── 5b) Bulk auto-match: mark multiple invoices paid ────────────────

    def auto_match_bulk_invoices(
        self,
        *,
        invoice_ids: list[int],
        payment_id: int,
    ) -> None:
        """Mark multiple invoices as paid in a single transaction.

        Used when a bulk payment covers several invoices for the same buyer.
        All updates are atomic — any failure triggers a full rollback.

        Args:
            invoice_ids: PKs of the invoices to mark as paid.
            payment_id: PK of the payment to mark as allocated.
        """
        with Session(self._engine) as session:
            with session.begin():
                for inv_id in invoice_ids:
                    invoice = session.get(InvoiceRow, inv_id)
                    if invoice is None:
                        raise ValueError(f"Invoice {inv_id} not found.")
                    invoice.status = "Paid"
                    invoice.recon_status = "IRIS_CONFIRMED"

                payment = session.get(PaymentRow, payment_id)
                if payment is None:
                    raise ValueError(f"Payment {payment_id} not found.")
                payment.allocation_status = "ALLOCATED"

        logger.info(
            "auto_match_bulk_invoices(invoices=%s, payment=%d) → IRIS_CONFIRMED",
            invoice_ids,
            payment_id,
        )

    # ── 5c) Fetch open invoices for bulk matching ──────────────────────

    def fetch_open_invoices_for_bulk(
        self,
        company_code: str = "NFL",
    ) -> list[CandidateInvoice]:
        """Fetch all open invoices for bulk subset-sum matching.

        Returns all Unpaid/Partially Paid invoices with their aggregated
        sales values. Used by the bulk matching engine to find combinations.
        """
        return self.fetch_open_invoices(company_code=company_code)

    # ── 6) Fetch pending manual reviews ─────────────────────────────────

    def fetch_pending_reviews(self) -> list[dict]:
        """Retrieve all manual reviews with ``review_status == 'PENDING'``.

        Returns:
            A list of dictionaries with review data.
        """
        from sqlalchemy.orm import joinedload

        stmt = (
            select(ManualReviewRow)
            .options(
                joinedload(ManualReviewRow.invoice),
                joinedload(ManualReviewRow.payment),
            )
            .where(ManualReviewRow.review_status == "PENDING")
            .order_by(ManualReviewRow.id)
        )

        results: list[dict] = []
        with Session(self._engine) as session:
            rows: Sequence[ManualReviewRow] = (
                session.execute(stmt).unique().scalars().all()
            )
            for row in rows:
                results.append({
                    "review_id": row.id,
                    "invoice_id": row.invoice_id,
                    "payment_id": row.payment_id,
                    "invoice_number": row.invoice.invoice_number if row.invoice else "N/A",
                    "payment_reference": row.payment.reference if row.payment else "N/A",
                    "invoice_amount": float(sum(d.sales_value for d in row.invoice.details)) if row.invoice else 0.0,
                    "payment_amount": float(row.payment.amount) if row.payment else 0.0,
                    "invoice_date": row.invoice.issue_date.isoformat() if row.invoice and row.invoice.issue_date else "N/A",
                    "payment_date": row.payment.payment_date.isoformat() if row.payment and row.payment.payment_date else "N/A",
                    "buyer_name": row.invoice.buyer_business_name if row.invoice else "N/A",
                    "sender_name": row.payment.sender_name if row.payment else "N/A",
                    "amount_score": float(row.amount_score),
                    "reference_score": float(row.reference_score),
                    "timing_score": float(row.timing_score),
                    "entity_score": float(row.entity_score),
                    "weighted_confidence": float(row.weighted_confidence),
                    "score_details": (
                        json.loads(row.score_details)
                        if row.score_details else {}
                    ),
                    "review_status": row.review_status,
                })

        logger.info(
            "fetch_pending_reviews() → %d review(s)", len(results)
        )
        return results

    def approve_manual_review(self, *, review_id: int) -> dict[str, int]:
        """Approve a pending manual review and allocate the match."""
        with Session(self._engine) as session:
            with session.begin():
                review = session.get(ManualReviewRow, review_id)
                if review is None:
                    raise ValueError(f"Manual review {review_id} not found.")
                if review.review_status != "PENDING":
                    raise ValueError(
                        f"Manual review {review_id} is not pending."
                    )

                invoice = session.get(InvoiceRow, review.invoice_id)
                if invoice is None:
                    raise ValueError(
                        f"Invoice {review.invoice_id} not found for review {review_id}."
                    )

                payment = session.get(PaymentRow, review.payment_id)
                if payment is None:
                    raise ValueError(
                        f"Payment {review.payment_id} not found for review {review_id}."
                    )

                invoice.status = "Paid"
                invoice.recon_status = "IRIS_CONFIRMED"
                payment.allocation_status = "ALLOCATED"
                review.review_status = "APPROVED"

                invoice_id = review.invoice_id
                payment_id = review.payment_id

        logger.info(
            "approve_manual_review(review=%d, invoice=%d, payment=%d)",
            review_id,
            invoice_id,
            payment_id,
        )
        return {
            "invoice_id": invoice_id,
            "payment_id": payment_id,
        }

    def reject_manual_review(self, *, review_id: int) -> dict[str, int]:
        """Reject a pending manual review without allocating the payment."""
        with Session(self._engine) as session:
            with session.begin():
                review = session.get(ManualReviewRow, review_id)
                if review is None:
                    raise ValueError(f"Manual review {review_id} not found.")
                if review.review_status != "PENDING":
                    raise ValueError(
                        f"Manual review {review_id} is not pending."
                    )

                invoice = session.get(InvoiceRow, review.invoice_id)
                if invoice is None:
                    raise ValueError(
                        f"Invoice {review.invoice_id} not found for review {review_id}."
                    )

                review.review_status = "REJECTED"
                invoice.recon_status = None

                invoice_id = review.invoice_id
                payment_id = review.payment_id

        logger.info(
            "reject_manual_review(review=%d, invoice=%d, payment=%d)",
            review_id,
            invoice_id,
            payment_id,
        )
        return {
            "invoice_id": invoice_id,
            "payment_id": payment_id,
        }


# ── Convenience: match a payment end-to-end ──────────────────────────────

def match_payment(
    dal: InvoiceDAL,
    *,
    payment_amount: Decimal,
    payment_date: date,
    sender_name: str,
    psid: str,
    company_code: str = "NFL",
) -> list[tuple[CandidateInvoice, "ReconciliationResult"]]:
    """Full pipeline: fetch candidates → score each → return sorted matches.

    This is the primary entry-point that ties the DAL to the reconciliation
    engine.  Results are returned sorted by ``weighted_confidence_index``
    descending (best match first).

    Returns:
        A list of ``(CandidateInvoice, ReconciliationResult)`` tuples.
    """
    from models import Invoice, Payment

    from reconciliation_service import ReconciliationService

    service = ReconciliationService()

    candidates = dal.find_candidates_for_payment(
        payment_amount=payment_amount,
        payment_date=payment_date,
        sender_name=sender_name,
        psid=psid,
        company_code=company_code,
    )

    payment = Payment(
        reference=psid,
        amount=payment_amount,
        payment_date=payment_date,
        sender_name=sender_name,
    )

    scored: list[tuple[CandidateInvoice, "ReconciliationResult"]] = []
    for candidate in candidates:
        invoice = Invoice(
            invoice_number=candidate.invoice_number,
            amount=candidate.total_sales_value,
            issue_date=candidate.issue_date,
            due_date=candidate.due_date,
            buyer_business_name=candidate.buyer_business_name,
            currency=candidate.currency,
        )
        result = service.reconcile(invoice, payment)
        scored.append((candidate, result))

    # Best match first
    scored.sort(key=lambda pair: pair[1].weighted_confidence_index, reverse=True)
    return scored
