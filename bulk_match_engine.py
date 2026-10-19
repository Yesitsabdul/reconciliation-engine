"""
Bulk Payment Matching Engine
=============================
Finds subsets of invoices (grouped by buyer) whose total_sales_value
sums to a target payment amount within a configurable tolerance.
Uses a backtracking subset-sum algorithm with pruning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List

from invoice_dal import CandidateInvoice

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BulkMatchResult:
    """An immutable record of a matched invoice group."""

    invoices: list[CandidateInvoice]
    buyer_identifier: str
    combined_amount: Decimal
    deviation_pct: float
    confidence: float = 0.95


def _get_buyer_key(candidate: CandidateInvoice) -> str:
    """Return the best available buyer identifier for grouping."""
    return candidate.buyer_iden_code if candidate.buyer_iden_code else candidate.buyer_business_name


def _backtrack(
    candidates: list[CandidateInvoice],
    index: int,
    current_combo: list[CandidateInvoice],
    current_sum: Decimal,
    target: Decimal,
    tolerance_abs: Decimal,
    min_group: int,
    max_group: int,
    results: list[list[CandidateInvoice]],
) -> None:
    """Recursively search for invoice subsets that sum within tolerance of target.

    Candidates must be pre-sorted descending by total_sales_value so the
    pruning condition (current_sum > target + tolerance_abs) is effective:
    once the running sum overshoots, no point exploring further since all
    remaining values are positive.

    Args:
        candidates:    Invoices for a single buyer, sorted desc by amount.
        index:         Position in *candidates* to consider next.
        current_combo: Invoices selected so far (mutable working list).
        current_sum:   Running total of selected invoices.
        target:        Desired payment amount.
        tolerance_abs: Absolute tolerance derived from target * tolerance_pct.
        min_group:     Minimum number of invoices in a valid combination.
        max_group:     Maximum number of invoices in a valid combination.
        results:       Accumulator — valid combinations are appended here.
    """
    # Prune: sum already exceeds the upper bound (all remaining are positive)
    if current_sum > target + tolerance_abs:
        return

    # Check if current combination is a valid match
    if len(current_combo) >= min_group and abs(current_sum - target) <= tolerance_abs:
        results.append(list(current_combo))

    # Stop expanding if we've hit the maximum group size
    if len(current_combo) >= max_group:
        return

    for i in range(index, len(candidates)):
        current_combo.append(candidates[i])
        _backtrack(
            candidates,
            i + 1,
            current_combo,
            current_sum + candidates[i].total_sales_value,
            target,
            tolerance_abs,
            min_group,
            max_group,
            results,
        )
        current_combo.pop()


def find_bulk_combinations(
    invoices: list[CandidateInvoice],
    target_amount: Decimal,
    tolerance_pct: float = 0.005,
    min_group: int = 2,
    max_group: int = 5,
) -> list[BulkMatchResult]:
    """Find groups of invoices whose amounts sum close to *target_amount*.

    Invoices are first partitioned by buyer (via ``_get_buyer_key``).
    Within each buyer group a backtracking subset-sum search finds all
    combinations of *min_group* to *max_group* invoices whose total is
    within ±*tolerance_pct* of *target_amount*.

    Args:
        invoices:      Pool of candidate invoices to search.
        target_amount: The payment amount to match against.
        tolerance_pct: Acceptable relative deviation (default 0.5 %).
        min_group:     Minimum invoices per combination (default 2).
        max_group:     Maximum invoices per combination (default 5).

    Returns:
        A list of :class:`BulkMatchResult` sorted by ``deviation_pct``
        ascending (closest match first).
    """
    if not invoices:
        logger.debug("No invoices provided — returning empty results.")
        return []

    tolerance_abs = target_amount * Decimal(str(tolerance_pct))
    logger.info(
        "Searching %d invoices for combinations summing to %s (±%s)",
        len(invoices),
        target_amount,
        tolerance_abs,
    )

    # Group invoices by buyer
    buyer_groups: dict[str, list[CandidateInvoice]] = {}
    for inv in invoices:
        key = _get_buyer_key(inv)
        buyer_groups.setdefault(key, []).append(inv)

    logger.debug("Invoices grouped into %d buyer(s).", len(buyer_groups))

    bulk_results: list[BulkMatchResult] = []

    for buyer_key, group in buyer_groups.items():
        # Sort descending by amount for effective pruning
        group.sort(key=lambda c: c.total_sales_value, reverse=True)

        combos: list[list[CandidateInvoice]] = []
        _backtrack(group, 0, [], Decimal("0"), target_amount, tolerance_abs, min_group, max_group, combos)

        logger.debug("Buyer '%s': found %d matching combination(s).", buyer_key, len(combos))

        for combo in combos:
            combined = sum((c.total_sales_value for c in combo), Decimal("0"))
            deviation = float(abs(combined - target_amount) / target_amount) if target_amount else 0.0
            bulk_results.append(
                BulkMatchResult(
                    invoices=combo,
                    buyer_identifier=buyer_key,
                    combined_amount=combined,
                    deviation_pct=deviation,
                )
            )

    bulk_results.sort(key=lambda r: r.deviation_pct)
    logger.info("Total bulk match results: %d", len(bulk_results))
    return bulk_results
