"""
REST API controller for the invoice reconciliation engine.

Endpoints
─────────
POST /api/reconcile/run     Trigger the matching engine for all unallocated
                            payments.  Returns a JSON summary of auto-matches,
                            flagged reviews, and discarded attempts.

GET  /api/reconcile/reviews List all pending manual reviews.

GET  /health                Liveness check.

Usage::

    # Install dependencies:
    #   pip install fastapi uvicorn sqlalchemy

    # Start the server:
    #   uvicorn api_controller:app --reload

    # Trigger reconciliation:
    #   curl -X POST http://localhost:8000/api/reconcile/run
"""

from __future__ import annotations

import csv
import io
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import Session

from db_models import (
    Base, InvoiceRow, InvoiceDetailRow, PaymentRow, ManualReviewRow,
)
from invoice_dal import InvoiceDAL
from models import Invoice, Payment, ReconciliationResult
from reconciliation_service import ReconciliationService, ReconciliationConfig
from bulk_match_engine import find_bulk_combinations

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

DATABASE_URL: str = os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        tmp_db = "/tmp/reconciliation.db"
        repo_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reconciliation.db")
        if os.path.exists(repo_db) and not os.path.exists(tmp_db):
            shutil.copyfile(repo_db, tmp_db)
        DATABASE_URL = f"sqlite:///{tmp_db}"
    else:
        DATABASE_URL = "sqlite:///reconciliation.db"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

COMPANY_CODE: str = "NFL"

# Triage thresholds
AUTO_MATCH_THRESHOLD: float = 0.85
REVIEW_THRESHOLD: float = 0.50
BULK_MATCH_CONFIDENCE: float = 0.95


# ── Dependency setup ─────────────────────────────────────────────────────────

def _create_engine(url: str = DATABASE_URL) -> Engine:
    """Create a SQLAlchemy engine.  Override for testing."""
    return create_engine(url, echo=False)


# Module-level singletons (replaced during testing via app.state)
_engine: Engine | None = None
_dal: InvoiceDAL | None = None


def get_engine() -> Engine:
    """Return the current engine, creating one lazily if needed."""
    global _engine
    if _engine is None:
        _engine = _create_engine()
        Base.metadata.create_all(_engine)
    return _engine


def get_dal() -> InvoiceDAL:
    """Return the current DAL instance."""
    global _dal
    if _dal is None:
        _dal = InvoiceDAL(get_engine())
    return _dal


def configure(engine: Engine) -> None:
    """Replace the module-level engine and DAL (used by tests)."""
    global _engine, _dal
    _engine = engine
    _dal = InvoiceDAL(engine)


# ── Response schemas ─────────────────────────────────────────────────────────

class AutoMatchItem(BaseModel):
    """A single auto-matched invoice–payment pair."""
    invoice_id: int
    invoice_number: str
    payment_id: int
    payment_reference: str
    weighted_confidence: float
    action: str = "AUTO_MATCHED"


class ReviewFlagItem(BaseModel):
    """A match flagged for human review."""
    review_id: int
    invoice_id: int
    invoice_number: str
    payment_id: int
    payment_reference: str
    weighted_confidence: float
    score_breakdown: dict[str, Any]
    action: str = "FLAGGED_FOR_REVIEW"


class ReviewDecisionRequest(BaseModel):
    """Request payload for a manual review decision."""
    action: Literal["MATCH", "DECLINE"]


class ReviewDecisionResult(BaseModel):
    """Result of applying a manual review decision."""
    review_id: int
    invoice_id: int
    payment_id: int
    review_status: str
    action: Literal["MATCH", "DECLINE"]


class DiscardedSummary(BaseModel):
    """Summary of discarded (low-score) attempts."""
    payment_id: int
    payment_reference: str
    candidates_evaluated: int
    best_score: float


class BulkMatchItem(BaseModel):
    """A group of invoices bulk-matched to a single payment."""
    invoice_ids: list[int]
    invoice_numbers: list[str]
    payment_id: int
    payment_reference: str
    buyer_identifier: str
    combined_amount: float
    deviation_pct: float
    weighted_confidence: float
    action: str = "BULK_AUTO_MATCHED"


class ReconcileRunRequest(BaseModel):
    """Overrides for engine weights and thresholds."""
    amount_weight: float = Field(default=0.45, ge=0.0, le=1.0)
    reference_weight: float = Field(default=0.30, ge=0.0, le=1.0)
    timing_weight: float = Field(default=0.15, ge=0.0, le=1.0)
    entity_weight: float = Field(default=0.10, ge=0.0, le=1.0)
    auto_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.50, ge=0.0, le=1.0)


class ReconcileRunResponse(BaseModel):
    """Full response from POST /api/reconcile/run."""
    payments_processed: int
    auto_matched: list[AutoMatchItem]
    bulk_matched: list[BulkMatchItem]
    flagged_for_review: list[ReviewFlagItem]
    discarded: list[DiscardedSummary]
    summary: dict[str, int]


# ── FastAPI application ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure the database schema exists on startup."""
    get_engine()
    yield


app = FastAPI(
    title="Reconciliation Engine",
    description="Matches incoming bank payments to open invoices using a "
                "Weighted Confidence Index scoring engine.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.post(
    "/api/reconcile/run",
    response_model=ReconcileRunResponse,
    summary="Run the reconciliation engine",
    description=(
        "Fetches all unallocated payments, finds candidate invoices for "
        "each, scores them via the Weighted Confidence Index engine, and "
        "triages the results into auto-match, review, or discard buckets."
    ),
)
async def reconcile_run(request: ReconcileRunRequest = None) -> ReconcileRunResponse:
    """Execute the full reconciliation pipeline."""
    if request is None:
        request = ReconcileRunRequest()
    dal = get_dal()
    
    config = ReconciliationConfig(
        amount_weight=request.amount_weight,
        reference_weight=request.reference_weight,
        timing_weight=request.timing_weight,
        entity_weight=request.entity_weight,
    )
    service = ReconciliationService(config)

    # 1. Fetch all unallocated payments
    payments = dal.fetch_unallocated_payments()

    if not payments:
        return ReconcileRunResponse(
            payments_processed=0,
            auto_matched=[],
            bulk_matched=[],
            flagged_for_review=[],
            discarded=[],
            summary={
                "auto_matched": 0,
                "bulk_matched": 0,
                "flagged_for_review": 0,
                "discarded": 0,
            },
        )

    auto_matched: list[AutoMatchItem] = []
    bulk_matched: list[BulkMatchItem] = []
    flagged_for_review: list[ReviewFlagItem] = []
    discarded: list[DiscardedSummary] = []

    # Pre-fetch all open invoices for bulk matching (used if needed)
    all_open_invoices = None  # lazy-loaded

    # 2. For each payment, find candidates and score them
    for pmt in payments:
        candidates = dal.find_candidates_for_payment(
            payment_amount=pmt.amount,
            payment_date=pmt.payment_date,
            sender_name=pmt.sender_name,
            psid=pmt.reference,
            company_code=COMPANY_CODE,
        )

        if not candidates:
            # No single-invoice candidates — try bulk matching
            if all_open_invoices is None:
                all_open_invoices = dal.fetch_open_invoices_for_bulk(
                    company_code=COMPANY_CODE,
                )

            bulk_results = find_bulk_combinations(
                invoices=all_open_invoices,
                target_amount=pmt.amount,
            )

            if bulk_results:
                best_bulk = bulk_results[0]
                dal.auto_match_bulk_invoices(
                    invoice_ids=[inv.invoice_id for inv in best_bulk.invoices],
                    payment_id=pmt.payment_id,
                )
                bulk_matched.append(
                    BulkMatchItem(
                        invoice_ids=[inv.invoice_id for inv in best_bulk.invoices],
                        invoice_numbers=[inv.invoice_number for inv in best_bulk.invoices],
                        payment_id=pmt.payment_id,
                        payment_reference=pmt.reference,
                        buyer_identifier=best_bulk.buyer_identifier,
                        combined_amount=float(best_bulk.combined_amount),
                        deviation_pct=best_bulk.deviation_pct,
                        weighted_confidence=best_bulk.confidence,
                    )
                )
                # Remove matched invoices from future bulk attempts
                matched_ids = {inv.invoice_id for inv in best_bulk.invoices}
                all_open_invoices = [
                    inv for inv in all_open_invoices
                    if inv.invoice_id not in matched_ids
                ]
            else:
                discarded.append(
                    DiscardedSummary(
                        payment_id=pmt.payment_id,
                        payment_reference=pmt.reference,
                        candidates_evaluated=0,
                        best_score=0.0,
                    )
                )
            continue

        # Build a Payment domain object for scoring
        payment_obj = Payment(
            reference=pmt.reference,
            amount=pmt.amount,
            payment_date=pmt.payment_date,
            sender_name=pmt.sender_name,
            currency=pmt.currency,
        )

        # Score each candidate
        scored: list[tuple[Any, ReconciliationResult]] = []
        for candidate in candidates:
            invoice_obj = Invoice(
                invoice_number=candidate.invoice_number,
                amount=candidate.total_sales_value,
                issue_date=candidate.issue_date,
                due_date=candidate.due_date,
                buyer_business_name=candidate.buyer_business_name,
                currency=candidate.currency,
            )
            result = service.reconcile(invoice_obj, payment_obj)
            scored.append((candidate, result))

        # Sort by WCI descending — best match first
        scored.sort(
            key=lambda pair: pair[1].weighted_confidence_index,
            reverse=True,
        )

        best_candidate, best_result = scored[0]
        best_wci = best_result.weighted_confidence_index

        # ── Triage switch ────────────────────────────────────────────────
        if best_wci >= request.auto_match_threshold:
            # AUTO-MATCH: update invoice → Paid, recon_status → IRIS_CONFIRMED
            dal.auto_match_invoice(
                invoice_id=best_candidate.invoice_id,
                payment_id=pmt.payment_id,
            )
            auto_matched.append(
                AutoMatchItem(
                    invoice_id=best_candidate.invoice_id,
                    invoice_number=best_candidate.invoice_number,
                    payment_id=pmt.payment_id,
                    payment_reference=pmt.reference,
                    weighted_confidence=round(best_wci, 6),
                )
            )

        elif best_wci >= request.review_threshold:
            # No strong single match — try bulk matching before flagging
            bulk_attempted = False
            if all_open_invoices is None:
                all_open_invoices = dal.fetch_open_invoices_for_bulk(
                    company_code=COMPANY_CODE,
                )

            bulk_results = find_bulk_combinations(
                invoices=all_open_invoices,
                target_amount=pmt.amount,
            )

            if bulk_results:
                best_bulk = bulk_results[0]
                dal.auto_match_bulk_invoices(
                    invoice_ids=[inv.invoice_id for inv in best_bulk.invoices],
                    payment_id=pmt.payment_id,
                )
                bulk_matched.append(
                    BulkMatchItem(
                        invoice_ids=[inv.invoice_id for inv in best_bulk.invoices],
                        invoice_numbers=[inv.invoice_number for inv in best_bulk.invoices],
                        payment_id=pmt.payment_id,
                        payment_reference=pmt.reference,
                        buyer_identifier=best_bulk.buyer_identifier,
                        combined_amount=float(best_bulk.combined_amount),
                        deviation_pct=best_bulk.deviation_pct,
                        weighted_confidence=best_bulk.confidence,
                    )
                )
                matched_ids = {inv.invoice_id for inv in best_bulk.invoices}
                all_open_invoices = [
                    inv for inv in all_open_invoices
                    if inv.invoice_id not in matched_ids
                ]
                bulk_attempted = True

            if not bulk_attempted:
                # FLAG FOR REVIEW: save to manual_reviews table
                review_id = dal.save_manual_review(
                    invoice_id=best_candidate.invoice_id,
                    payment_id=pmt.payment_id,
                    result=best_result,
                )
                flagged_for_review.append(
                    ReviewFlagItem(
                        review_id=review_id,
                        invoice_id=best_candidate.invoice_id,
                        invoice_number=best_candidate.invoice_number,
                        payment_id=pmt.payment_id,
                        payment_reference=pmt.reference,
                        weighted_confidence=round(best_wci, 6),
                        score_breakdown={
                            "amount_score": best_result.amount_score,
                            "reference_score": best_result.reference_score,
                            "timing_score": best_result.timing_score,
                            "entity_score": best_result.entity_score,
                            "details": best_result.details,
                        },
                    )
                )

        else:
            # Score < 0.50 — try bulk matching before discarding
            if all_open_invoices is None:
                all_open_invoices = dal.fetch_open_invoices_for_bulk(
                    company_code=COMPANY_CODE,
                )

            bulk_results = find_bulk_combinations(
                invoices=all_open_invoices,
                target_amount=pmt.amount,
            )

            if bulk_results:
                best_bulk = bulk_results[0]
                dal.auto_match_bulk_invoices(
                    invoice_ids=[inv.invoice_id for inv in best_bulk.invoices],
                    payment_id=pmt.payment_id,
                )
                bulk_matched.append(
                    BulkMatchItem(
                        invoice_ids=[inv.invoice_id for inv in best_bulk.invoices],
                        invoice_numbers=[inv.invoice_number for inv in best_bulk.invoices],
                        payment_id=pmt.payment_id,
                        payment_reference=pmt.reference,
                        buyer_identifier=best_bulk.buyer_identifier,
                        combined_amount=float(best_bulk.combined_amount),
                        deviation_pct=best_bulk.deviation_pct,
                        weighted_confidence=best_bulk.confidence,
                    )
                )
                matched_ids = {inv.invoice_id for inv in best_bulk.invoices}
                all_open_invoices = [
                    inv for inv in all_open_invoices
                    if inv.invoice_id not in matched_ids
                ]
            else:
                # DISCARD: score too low and no bulk match
                discarded.append(
                    DiscardedSummary(
                        payment_id=pmt.payment_id,
                        payment_reference=pmt.reference,
                        candidates_evaluated=len(scored),
                        best_score=round(best_wci, 6),
                    )
                )

    return ReconcileRunResponse(
        payments_processed=len(payments),
        auto_matched=auto_matched,
        bulk_matched=bulk_matched,
        flagged_for_review=flagged_for_review,
        discarded=discarded,
        summary={
            "auto_matched": len(auto_matched),
            "bulk_matched": len(bulk_matched),
            "flagged_for_review": len(flagged_for_review),
            "discarded": len(discarded),
        },
    )


# ── GET /api/reconcile/reviews ───────────────────────────────────────────────

@app.get(
    "/api/reconcile/reviews",
    summary="List pending manual reviews",
    description="Returns all invoice–payment pairs that require human review.",
)
async def list_reviews() -> list[dict]:
    """Return all pending manual reviews."""
    dal = get_dal()
    return dal.fetch_pending_reviews()


@app.post(
    "/api/reconcile/reviews/{review_id}/decision",
    response_model=ReviewDecisionResult,
    summary="Apply a manual review decision",
    description=(
        "Approve or decline a flagged invoice–payment match after human review. "
        "MATCH will allocate the payment and mark the invoice paid; "
        "DECLINE will mark the review rejected and leave the payment unallocated."
    ),
)
async def decide_review(
    review_id: int,
    request: ReviewDecisionRequest,
) -> ReviewDecisionResult:
    """Apply a decision to a pending manual review."""
    dal = get_dal()

    try:
        if request.action == "MATCH":
            result = dal.approve_manual_review(review_id=review_id)
            review_status = "APPROVED"
        else:
            result = dal.reject_manual_review(review_id=review_id)
            review_status = "REJECTED"
    except ValueError as exc:
        message = str(exc)
        if "not found" in message:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)

    return ReviewDecisionResult(
        review_id=review_id,
        invoice_id=result["invoice_id"],
        payment_id=result["payment_id"],
        review_status=review_status,
        action=request.action,
    )


# ── GET /health ──────────────────────────────────────────────────────────────

@app.get(
    "/health",
    summary="Health check",
    description="Simple liveness probe.",
)
async def health_check() -> dict[str, str]:
    """Return a simple health status."""
    return {"status": "healthy", "service": "reconciliation-engine"}


# ── POST /api/upload/invoices ────────────────────────────────────────────

@app.post(
    "/api/upload/invoices",
    summary="Upload invoices CSV",
    description=(
        "Accepts a CSV file with columns: invoice_number, company_code, "
        "buyer_business_name, buyer_iden_code, issue_date, due_date, "
        "product_code, quantity, unit_price, sales_value. "
        "Replaces all existing invoice data."
    ),
)
async def upload_invoices(file: UploadFile = File(...)) -> dict:
    """Parse an invoices CSV and insert into the database."""
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    engine = get_engine()
    with Session(engine) as session:
        session.query(ManualReviewRow).delete()
        session.query(InvoiceDetailRow).delete()
        session.query(InvoiceRow).delete()
        session.commit()

        invoices: dict[str, InvoiceRow] = {}
        details: list[InvoiceDetailRow] = []
        inv_id = 1
        detail_id = 1

        for row in reader:
            inv_num = row["invoice_number"].strip()
            if inv_num not in invoices:
                invoices[inv_num] = InvoiceRow(
                    id=inv_id,
                    invoice_number=inv_num,
                    company_code=row.get("company_code", "NFL").strip() or "NFL",
                    buyer_business_name=row["buyer_business_name"].strip(),
                    buyer_iden_code=(
                        (row.get("buyer_iden_code") or "").strip() or None
                    ),
                    issue_date=date.fromisoformat(row["issue_date"].strip()),
                    due_date=date.fromisoformat(row["due_date"].strip()),
                    status="Unpaid",
                )
                inv_id += 1

            parent = invoices[inv_num]
            details.append(
                InvoiceDetailRow(
                    id=detail_id,
                    invoice_id=parent.id,
                    product_code=row["product_code"].strip(),
                    quantity=int(row["quantity"]),
                    unit_price=Decimal(row["unit_price"].strip()),
                    sales_value=Decimal(row["sales_value"].strip()),
                )
            )
            detail_id += 1

        session.add_all(invoices.values())
        session.add_all(details)
        session.commit()

    return {
        "status": "success",
        "invoices_created": len(invoices),
        "detail_rows_created": len(details),
    }


# ── POST /api/upload/payments ────────────────────────────────────────────

@app.post(
    "/api/upload/payments",
    summary="Upload payments CSV",
    description=(
        "Accepts a CSV file with columns: reference, amount, payment_date, "
        "sender_name. Replaces all existing payment data."
    ),
)
async def upload_payments(file: UploadFile = File(...)) -> dict:
    """Parse a payments CSV and insert into the database."""
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    engine = get_engine()
    with Session(engine) as session:
        session.query(ManualReviewRow).delete()
        session.query(PaymentRow).delete()
        session.commit()

        payments: list[PaymentRow] = []
        pmt_id = 1

        for row in reader:
            payments.append(
                PaymentRow(
                    id=pmt_id,
                    reference=row["reference"].strip(),
                    amount=Decimal(row["amount"].strip()),
                    payment_date=date.fromisoformat(
                        row["payment_date"].strip()
                    ),
                    sender_name=row["sender_name"].strip(),
                    allocation_status="UNALLOCATED",
                )
            )
            pmt_id += 1

        session.add_all(payments)
        session.commit()

    return {
        "status": "success",
        "payments_created": len(payments),
    }


# ── Static frontend assets ──────────────────────────────────────────────────
_frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
if os.path.exists(_frontend_path):
    app.mount("/", StaticFiles(directory=_frontend_path, html=True), name="frontend")

