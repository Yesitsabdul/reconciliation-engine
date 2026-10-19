"""
SQLAlchemy ORM models mapping to the MySQL ``invoices`` and ``invoice_details``
tables.

These models mirror an existing database schema.  The composite index
``idx_invoices_company_code_status`` on (company_code, status) is declared
here so the ORM is aware of it, but in production it already exists on the
MySQL side.

Table layout
────────────
invoices
  ├─ id                    BIGINT PK AUTO_INCREMENT
  ├─ invoice_number        VARCHAR(50) UNIQUE NOT NULL
  ├─ company_code          VARCHAR(20) NOT NULL
  ├─ status                VARCHAR(20) NOT NULL   -- Unpaid | Partially Paid | Paid
  ├─ buyer_business_name   VARCHAR(255) NOT NULL
  ├─ issue_date            DATE NOT NULL
  ├─ due_date              DATE NOT NULL
  ├─ currency              VARCHAR(3) DEFAULT 'USD'
  ├─ created_at            DATETIME
  └─ updated_at            DATETIME

invoice_details
  ├─ id                    BIGINT PK AUTO_INCREMENT
  ├─ invoice_id            BIGINT FK → invoices.id
  ├─ product_code          VARCHAR(50) NOT NULL
  ├─ quantity              INT NOT NULL
  ├─ unit_price            DECIMAL(15,2) NOT NULL
  └─ sales_value           DECIMAL(15,2) NOT NULL  -- quantity × unit_price
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ── Base ─────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


# ── Invoices ─────────────────────────────────────────────────────────────────

class InvoiceRow(Base):
    """ORM model for the ``invoices`` table."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    company_code: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    buyer_business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    buyer_iden_code: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, default=None,
        doc="Stable buyer identifier for grouping invoices in bulk matching.",
    )
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    recon_status: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, default=None,
        doc="Reconciliation outcome: NULL | IRIS_CONFIRMED | REVIEW_FLAGGED",
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )

    # One-to-many → invoice_details
    details: Mapped[list["InvoiceDetailRow"]] = relationship(
        back_populates="invoice",
        lazy="joined",      # eager-load details in a single query
    )

    # ── Composite index the query optimizer must hit ─────────────────────
    __table_args__ = (
        Index("idx_invoices_company_code_status", "company_code", "status"),
        Index("idx_invoices_buyer_iden_code_status", "buyer_iden_code", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<InvoiceRow id={self.id} invoice_number='{self.invoice_number}' "
            f"status='{self.status}'>"
        )


# ── Invoice Details ──────────────────────────────────────────────────────────

class InvoiceDetailRow(Base):
    """ORM model for the ``invoice_details`` table."""

    __tablename__ = "invoice_details"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    product_code: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    sales_value: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)

    # Back-reference
    invoice: Mapped["InvoiceRow"] = relationship(back_populates="details")

    # ── Index for FK look-ups ────────────────────────────────────────────
    __table_args__ = (
        Index("idx_details_invoice_id", "invoice_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<InvoiceDetailRow id={self.id} product='{self.product_code}' "
            f"sales_value={self.sales_value}>"
        )


# ── Payments ─────────────────────────────────────────────────────────────────

class PaymentRow(Base):
    """ORM model for the ``payments`` table.

    Tracks incoming bank payments and their allocation status.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reference: Mapped[str] = mapped_column(
        String(100), nullable=False, doc="PSID / payment reference from the bank",
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    allocation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNALLOCATED",
        doc="UNALLOCATED | ALLOCATED",
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(),
    )

    # ── Index for fetching unallocated payments ──────────────────────────
    __table_args__ = (
        Index("idx_payments_allocation_status", "allocation_status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PaymentRow id={self.id} ref='{self.reference}' "
            f"amount={self.amount} status='{self.allocation_status}'>"
        )


# ── Manual Reviews ───────────────────────────────────────────────────────────

class ManualReviewRow(Base):
    """ORM model for the ``manual_reviews`` table.

    Stores invoice–payment pairs that scored between 0.50 and 0.85 on the
    Weighted Confidence Index and require human review.
    """

    __tablename__ = "manual_reviews"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    invoice_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False,
    )
    payment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("payments.id", ondelete="CASCADE"), nullable=False,
    )
    amount_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    reference_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    timing_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    entity_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    weighted_confidence: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    score_details: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, doc="JSON-serialised per-vector explanations",
    )
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING",
        doc="PENDING | APPROVED | REJECTED",
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=func.now())

    # ── Relationships ────────────────────────────────────────────────────
    invoice: Mapped["InvoiceRow"] = relationship(foreign_keys=[invoice_id])
    payment: Mapped["PaymentRow"] = relationship(foreign_keys=[payment_id])

    # ── Indexes ──────────────────────────────────────────────────────────
    __table_args__ = (
        Index("idx_reviews_status", "review_status"),
        Index("idx_reviews_invoice_payment", "invoice_id", "payment_id", unique=True),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ManualReviewRow id={self.id} invoice={self.invoice_id} "
            f"payment={self.payment_id} wci={self.weighted_confidence}>"
        )
