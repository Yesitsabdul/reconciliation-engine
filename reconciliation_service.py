"""
Weighted Confidence Index reconciliation engine.

Compares an Invoice against a Payment across four scoring vectors and
produces a composite confidence score between 0.0 and 1.0.

Scoring Vectors
───────────────
 Vector      │ Weight │ Description
─────────────┼────────┼──────────────────────────────────────────────
 Amount      │  0.45  │ Exact-match → 1.0; within tolerance → linear
             │        │ decay; outside tolerance → 0.0.
 Reference   │  0.30  │ Fuzzy string similarity (Levenshtein-based)
             │        │ between normalised reference strings.
 Timing      │  0.15  │ Exponential decay over days elapsed after
             │        │ invoice issue date; pre-dated payments → 0.0.
 Entity      │  0.10  │ Fuzzy match of payer name vs buyer business
             │        │ name.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from models import Invoice, Payment, ReconciliationResult


# ── Helper: Levenshtein distance ────────────────────────────────────────────

def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein (edit) distance between two strings.

    Uses the classic dynamic-programming, O(m·n) approach with an optimised
    single-row buffer so memory usage is O(min(m, n)).
    """
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row: list[int] = list(range(len(s2) + 1))

    for i, c1 in enumerate(s1):
        current_row: list[int] = [i + 1]
        for j, c2 in enumerate(s2):
            # Insertions, deletions, substitutions
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (0 if c1 == c2 else 1)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def _levenshtein_similarity(s1: str, s2: str) -> float:
    """Return a similarity ratio in [0.0, 1.0] derived from Levenshtein distance."""
    if not s1 and not s2:
        return 1.0
    max_len = max(len(s1), len(s2))
    if max_len == 0:
        return 1.0
    distance = _levenshtein_distance(s1, s2)
    return 1.0 - (distance / max_len)


# ── Helper: string normalisation ────────────────────────────────────────────

_SPECIAL_CHARS_RE = re.compile(r"[^a-z0-9\s]")


def _normalise(text: str) -> str:
    """Lower-case, strip special characters, and collapse whitespace."""
    text = text.lower().strip()
    text = _SPECIAL_CHARS_RE.sub("", text)
    return " ".join(text.split())


# ── Configuration dataclass ─────────────────────────────────────────────────

@dataclass
class ReconciliationConfig:
    """Tunable knobs for the reconciliation engine.

    Attributes:
        amount_weight: Weight for the amount scoring vector.
        reference_weight: Weight for the reference scoring vector.
        timing_weight: Weight for the timing scoring vector.
        entity_weight: Weight for the entity scoring vector.
        amount_tolerance_pct: Maximum relative deviation tolerated before
            the amount score drops to 0 (e.g. 0.01 = 1 %).
        timing_decay_lambda: Lambda (λ) for exponential decay
            ``e^{-λ · days}``.  Higher values penalise late payments
            more aggressively.
        timing_max_days: Payments arriving more than this many days after
            the invoice issue date score 0.
    """

    amount_weight: float = 0.45
    reference_weight: float = 0.30
    timing_weight: float = 0.15
    entity_weight: float = 0.10

    amount_tolerance_pct: float = 0.01   # 1 %
    timing_decay_lambda: float = 0.05    # gentle decay over ~60 days
    timing_max_days: int = 180           # 6-month hard cut-off

    def __post_init__(self) -> None:
        total = (
            self.amount_weight
            + self.reference_weight
            + self.timing_weight
            + self.entity_weight
        )
        if not math.isclose(total, 1.0, rel_tol=1e-6):
            raise ValueError(
                f"Weights must sum to 1.0, got {total:.4f}."
            )


# ── Service ──────────────────────────────────────────────────────────────────

class ReconciliationService:
    """Calculates a Weighted Confidence Index between an Invoice and a Payment.

    Usage::

        service = ReconciliationService()          # uses default config
        result  = service.reconcile(invoice, payment)
        print(result.weighted_confidence_index)    # 0.0 – 1.0
    """

    def __init__(self, config: Optional[ReconciliationConfig] = None) -> None:
        self._cfg = config or ReconciliationConfig()

    # ── Public API ───────────────────────────────────────────────────────

    def reconcile(
        self,
        invoice: Invoice,
        payment: Payment,
    ) -> ReconciliationResult:
        """Run all four scoring vectors and return a :class:`ReconciliationResult`.

        Args:
            invoice: The invoice to reconcile against.
            payment: The incoming payment.

        Returns:
            A ``ReconciliationResult`` with individual and composite scores.
        """
        amount_score, amount_detail = self.score_amount(invoice, payment)
        reference_score, reference_detail = self.score_reference(invoice, payment)
        timing_score, timing_detail = self.score_timing(invoice, payment)
        entity_score, entity_detail = self.score_entity(invoice, payment)

        wci = (
            self._cfg.amount_weight * amount_score
            + self._cfg.reference_weight * reference_score
            + self._cfg.timing_weight * timing_score
            + self._cfg.entity_weight * entity_score
        )

        # Clamp to [0.0, 1.0] to guard against floating-point drift.
        wci = max(0.0, min(1.0, wci))

        return ReconciliationResult(
            amount_score=round(amount_score, 6),
            reference_score=round(reference_score, 6),
            timing_score=round(timing_score, 6),
            entity_score=round(entity_score, 6),
            weighted_confidence_index=round(wci, 6),
            details={
                "amount": amount_detail,
                "reference": reference_detail,
                "timing": timing_detail,
                "entity": entity_detail,
            },
        )

    # ── Vector 1: Amount (weight 0.45) ───────────────────────────────────

    def score_amount(
        self,
        invoice: Invoice,
        payment: Payment,
    ) -> tuple[float, str]:
        """Score the payment amount against the invoice amount.

        * Exact match → 1.0
        * Within tolerance (default 1 %) → linear decay:
          ``1 - |payment - invoice| / invoice``
        * Outside tolerance → 0.0

        Returns:
            (score, human_detail)
        """
        inv_amt = float(invoice.amount)
        pay_amt = float(payment.amount)

        if inv_amt == 0:
            return (0.0, "Invoice amount is zero; cannot score.")

        deviation = abs(pay_amt - inv_amt)
        relative_deviation = deviation / inv_amt

        if deviation == 0.0:
            return (1.0, "Exact amount match.")

        tolerance = self._cfg.amount_tolerance_pct
        if relative_deviation <= tolerance:
            score = 1.0 - (deviation / inv_amt)
            return (
                score,
                f"Within tolerance ({relative_deviation:.4%} deviation, "
                f"threshold {tolerance:.2%}). Decay applied.",
            )

        return (
            0.0,
            f"Outside tolerance ({relative_deviation:.4%} deviation, "
            f"threshold {tolerance:.2%}).",
        )

    # ── Vector 2: Reference (weight 0.30) ────────────────────────────────

    def score_reference(
        self,
        invoice: Invoice,
        payment: Payment,
    ) -> tuple[float, str]:
        """Fuzzy-match the payment reference against the invoice number.

        Both strings are normalised (lowered, special chars stripped) before
        comparison.  Similarity is computed via Levenshtein distance.

        Returns:
            (score, human_detail)
        """
        inv_ref = _normalise(invoice.invoice_number)
        pay_ref = _normalise(payment.reference)

        if not inv_ref and not pay_ref:
            return (0.0, "Both references are empty.")

        similarity = _levenshtein_similarity(inv_ref, pay_ref)

        if similarity == 1.0:
            detail = f"Exact reference match ('{pay_ref}')."
        else:
            distance = _levenshtein_distance(inv_ref, pay_ref)
            detail = (
                f"Fuzzy match: similarity={similarity:.4f}, "
                f"edit_distance={distance} "
                f"(inv='{inv_ref}', pay='{pay_ref}')."
            )

        return (similarity, detail)

    # ── Vector 3: Timing (weight 0.15) ────────────────────────────────────

    def score_timing(
        self,
        invoice: Invoice,
        payment: Payment,
    ) -> tuple[float, str]:
        """Score based on how many days elapsed between invoice date and payment.

        * Payment before invoice issue date → 0.0
        * Same day → 1.0
        * After issue date → exponential decay ``e^{-λ · days}``
        * Beyond ``timing_max_days`` → 0.0

        Returns:
            (score, human_detail)
        """
        day_diff = (payment.payment_date - invoice.issue_date).days

        if day_diff < 0:
            return (
                0.0,
                f"Payment predates invoice by {abs(day_diff)} day(s); scored 0.",
            )

        if day_diff == 0:
            return (1.0, "Payment on same day as invoice; perfect timing.")

        if day_diff > self._cfg.timing_max_days:
            return (
                0.0,
                f"Payment {day_diff} day(s) after invoice exceeds "
                f"max window ({self._cfg.timing_max_days} days).",
            )

        lam = self._cfg.timing_decay_lambda
        score = math.exp(-lam * day_diff)

        return (
            score,
            f"Payment {day_diff} day(s) after invoice. "
            f"Exponential decay (λ={lam}): score={score:.4f}.",
        )

    # ── Vector 4: Entity (weight 0.10) ────────────────────────────────────

    def score_entity(
        self,
        invoice: Invoice,
        payment: Payment,
    ) -> tuple[float, str]:
        """Fuzzy-match the payment sender name against the buyer business name.

        Both names are normalised before comparison.

        Returns:
            (score, human_detail)
        """
        inv_name = _normalise(invoice.buyer_business_name)
        pay_name = _normalise(payment.sender_name)

        if not inv_name and not pay_name:
            return (0.0, "Both entity names are empty.")

        similarity = _levenshtein_similarity(inv_name, pay_name)

        if similarity == 1.0:
            detail = f"Exact entity match ('{pay_name}')."
        else:
            distance = _levenshtein_distance(inv_name, pay_name)
            detail = (
                f"Fuzzy entity match: similarity={similarity:.4f}, "
                f"edit_distance={distance} "
                f"(invoice='{inv_name}', payment='{pay_name}')."
            )

        return (similarity, detail)
