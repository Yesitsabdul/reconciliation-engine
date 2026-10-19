"""
Data models for Invoice and Payment entities used in reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Invoice:
    """Represents an issued invoice awaiting payment.

    Attributes:
        invoice_number: Unique identifier / reference code for the invoice.
        amount: Total amount due (positive decimal).
        issue_date: The date the invoice was issued.
        due_date: The date payment is expected by.
        buyer_business_name: Legal or trading name of the buyer.
        currency: ISO 4217 currency code (default "USD").
    """

    invoice_number: str
    amount: Decimal
    issue_date: date
    due_date: date
    buyer_business_name: str
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("Invoice amount must be positive.")


@dataclass(frozen=True)
class Payment:
    """Represents an incoming payment received from a bank or payment gateway.

    Attributes:
        reference: The PSID / reference string attached to the payment.
        amount: The received payment amount (positive decimal).
        payment_date: The date the payment was received / settled.
        sender_name: Name of the payer as reported by the bank.
        currency: ISO 4217 currency code (default "USD").
    """

    reference: str
    amount: Decimal
    payment_date: date
    sender_name: str
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("Payment amount must be positive.")


@dataclass
class ReconciliationResult:
    """Holds the full breakdown of a reconciliation scoring pass.

    Attributes:
        amount_score: Raw score for the amount vector (0.0–1.0).
        reference_score: Raw score for the reference vector (0.0–1.0).
        timing_score: Raw score for the timing vector (0.0–1.0).
        entity_score: Raw score for the entity vector (0.0–1.0).
        weighted_confidence_index: Final weighted score (0.0–1.0).
        details: Human-readable explanations for each vector.
    """

    amount_score: float
    reference_score: float
    timing_score: float
    entity_score: float
    weighted_confidence_index: float
    details: dict[str, str] = field(default_factory=dict)

    @property
    def is_confident_match(self) -> bool:
        """Returns True when the weighted confidence index ≥ 0.85."""
        return self.weighted_confidence_index >= 0.85

    @property
    def is_probable_match(self) -> bool:
        """Returns True when the weighted confidence index ≥ 0.65."""
        return self.weighted_confidence_index >= 0.65
