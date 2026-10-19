-- =============================================================================
-- Invoice-to-Payment Reconciliation Engine — Raw SQL Reference
-- =============================================================================
-- These are the optimised queries that the SQLAlchemy DAL generates.
-- They are provided here for DBA review, EXPLAIN analysis, and direct
-- MySQL usage outside of Python.
--
-- Required index (must already exist):
--   CREATE INDEX idx_invoices_company_code_status
--       ON invoices (company_code, status);
--
-- Optional supporting index for the details FK join:
--   CREATE INDEX idx_details_invoice_id
--       ON invoice_details (invoice_id);
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- QUERY 1: Fetch all open invoices for company code 'NFL'
--           joined with invoice_details for product-level data.
--
-- Index usage: The WHERE clause is structured so that MySQL performs an
-- index-range scan on idx_invoices_company_code_status:
--   company_code = 'NFL'  →  seek to the 'NFL' partition
--   status IN (...)       →  scan only qualifying status values
-- ─────────────────────────────────────────────────────────────────────────────

SELECT
    i.id                    AS invoice_id,
    i.invoice_number,
    i.company_code,
    i.status,
    i.buyer_business_name,
    i.issue_date,
    i.due_date,
    i.currency,
    d.id                    AS detail_id,
    d.product_code,
    d.quantity,
    d.unit_price,
    d.sales_value
FROM invoices i
JOIN invoice_details d ON d.invoice_id = i.id
WHERE i.company_code = 'NFL'
  AND i.status IN ('Unpaid', 'Partially Paid')
ORDER BY i.id, d.id;


-- ─────────────────────────────────────────────────────────────────────────────
-- QUERY 2: Find candidate invoices for an incoming payment.
--
-- Parameters (bind variables in application code):
--   :payment_amount   — e.g. 10000.00
--   :variance_pct     — e.g. 0.20  (20%)
--   :sender_token_1   — e.g. '%acme%'
--   :sender_token_2   — e.g. '%corp%'
--   :company_code     — e.g. 'NFL'
--
-- This query uses a grouped JOIN so that SUM(d.sales_value) can be
-- filtered in the HAVING clause.  Two OR-ed criteria surface candidates:
--
--   1. Amount variance:  total sales_value within ±20% of payment_amount
--   2. Entity match:     buyer_business_name LIKE any sender name token
--
-- The base WHERE still restricts to (company_code, status) so the
-- composite index is used for the initial row seek.
-- ─────────────────────────────────────────────────────────────────────────────

SELECT
    i.id                                       AS invoice_id,
    i.invoice_number,
    i.buyer_business_name,
    i.issue_date,
    i.due_date,
    i.currency,
    COALESCE(SUM(d.sales_value), 0)            AS total_sales_value
FROM invoices i
JOIN invoice_details d ON d.invoice_id = i.id
WHERE i.company_code = 'NFL'                            -- ← index seek col 1
  AND i.status IN ('Unpaid', 'Partially Paid')          -- ← index seek col 2
GROUP BY
    i.id,
    i.invoice_number,
    i.buyer_business_name,
    i.issue_date,
    i.due_date,
    i.currency
HAVING
    -- Criterion 1: Amount within ±20%
    COALESCE(SUM(d.sales_value), 0)
        BETWEEN 10000.00 * (1 - 0.20) AND 10000.00 * (1 + 0.20)
    -- Criterion 2: Sender name token match
    OR LOWER(i.buyer_business_name) LIKE '%acme%'
    OR LOWER(i.buyer_business_name) LIKE '%corp%'
ORDER BY i.id;


-- ─────────────────────────────────────────────────────────────────────────────
-- EXPLAIN plan (run this against your MySQL instance to verify index usage):
-- ─────────────────────────────────────────────────────────────────────────────

-- EXPLAIN
-- SELECT ...  (paste either query above)
-- ;

-- Expected output should show:
--   type:     ref  or  range
--   key:      idx_invoices_company_code_status
--   key_len:  (length of company_code + status columns)
--   rows:     (small subset of total table)
