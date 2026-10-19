/* ═══════════════════════════════════════════════════════════════════════════
   UI Components — Reconciliation Engine

   Every render method returns an HTML string.
   Data shapes are normalised to handle both:
     • POST /api/reconcile/run → flagged_for_review items
     • GET  /api/reconcile/reviews → review list from DB
   ═══════════════════════════════════════════════════════════════════════════ */

const components = {

    /* ── Helpers ────────────────────────────────────────────────────────── */

    renderConfidenceBadge(score) {
        const pct = (score * 100).toFixed(1);
        let cls = 'danger';
        if (score >= 0.85) cls = 'success';
        else if (score >= 0.50) cls = 'warning';
        return `<span class="badge ${cls}">${pct}%</span>`;
    },

    renderScoreBar(label, score) {
        const pct = (score * 100).toFixed(0);
        let cls = 'low';
        if (score >= 0.8) cls = 'high';
        else if (score >= 0.5) cls = 'medium';
        return `
            <div class="score-bar-container">
                <div class="score-bar-label">
                    <span>${label}</span>
                    <span class="score-value">${pct}%</span>
                </div>
                <div class="score-bar-track">
                    <div class="score-bar-fill ${cls}" style="width: ${pct}%"></div>
                </div>
            </div>
        `;
    },

    renderMetricCard(label, value, footnote = null, footnoteType = 'neutral') {
        const footnoteHtml = footnote
            ? `<span class="badge ${footnoteType}">${footnote}</span>`
            : '';
        return `
            <div class="metric-card">
                <div class="metric-label">${label}</div>
                <div class="metric-value">${value}${footnoteHtml ? ' ' + footnoteHtml : ''}</div>
            </div>
        `;
    },

    renderEmptyState(title, description = '') {
        return `
            <div class="empty-state">
                <div class="empty-state-icon">
                    <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
                        <polyline points="13 2 13 9 20 9"/>
                    </svg>
                </div>
                <h3>${title}</h3>
                ${description ? `<p>${description}</p>` : ''}
            </div>
        `;
    },

    /* ── Auto-Match Table ──────────────────────────────────────────────── */

    renderAutoMatchTable(data) {
        if (!data || data.length === 0) {
            return this.renderEmptyState('No auto-matched items', 'Payments that score above the auto-match threshold appear here.');
        }

        const rows = data.map(item => `
            <tr>
                <td class="mono">${item.invoice_number}</td>
                <td class="mono">${item.payment_reference}</td>
                <td>${this.renderConfidenceBadge(item.weighted_confidence)}</td>
                <td><span class="badge success">Matched</span></td>
            </tr>
        `).join('');

        return `
            <table>
                <thead>
                    <tr>
                        <th>Invoice</th>
                        <th>Payment Ref</th>
                        <th>Confidence</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /* ── Bulk-Match Table ──────────────────────────────────────────────── */

    renderBulkMatchTable(data) {
        if (!data || data.length === 0) {
            return this.renderEmptyState('No bulk-matched items', 'Groups of invoices matched to a single payment appear here.');
        }

        const rows = data.map(item => `
            <tr>
                <td class="mono">${item.invoice_numbers.join(', ')}</td>
                <td class="mono">${item.payment_reference}</td>
                <td>$${item.combined_amount.toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                <td>${item.deviation_pct.toFixed(2)}%</td>
                <td>${this.renderConfidenceBadge(item.weighted_confidence)}</td>
            </tr>
        `).join('');

        return `
            <table>
                <thead>
                    <tr>
                        <th>Invoices</th>
                        <th>Payment Ref</th>
                        <th>Combined Amount</th>
                        <th>Deviation</th>
                        <th>Confidence</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /* ── Discarded Table ───────────────────────────────────────────────── */

    renderDiscardedTable(data) {
        if (!data || data.length === 0) {
            return this.renderEmptyState('No discarded payments', 'Payments that scored below the review threshold appear here.');
        }

        const rows = data.map(item => `
            <tr>
                <td class="mono">${item.payment_reference}</td>
                <td>${item.candidates_evaluated}</td>
                <td>${this.renderConfidenceBadge(item.best_score)}</td>
                <td><span class="badge danger">Discarded</span></td>
            </tr>
        `).join('');

        return `
            <table>
                <thead>
                    <tr>
                        <th>Payment Ref</th>
                        <th>Candidates Evaluated</th>
                        <th>Best Score</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    /* ── Flagged-for-Review Table (from POST run response) ─────────────── */

    renderFlaggedTable(data) {
        if (!data || data.length === 0) {
            return this.renderEmptyState('No flagged items', 'Payments requiring manual review appear here.');
        }

        const rows = data.map((item, i) => {
            const bd = item.score_breakdown || {};
            return `
                <tr class="review-row" data-index="${i}">
                    <td class="mono">${item.invoice_number}</td>
                    <td class="mono">${item.payment_reference}</td>
                    <td>${this.renderConfidenceBadge(item.weighted_confidence)}</td>
                    <td><span class="badge warning">Needs Review</span></td>
                    <td>
                        <button class="btn btn-success review-action" data-review-id="${item.review_id}" data-action="MATCH">Match</button>
                        <button class="btn btn-secondary review-action" data-review-id="${item.review_id}" data-action="DECLINE">Decline</button>
                    </td>
                </tr>
                <tr class="review-details-row">
                    <td colspan="5" style="padding: 0; border: none;">
                        <div class="review-details">
                            <h4>Score Breakdown</h4>
                            <div class="scores-grid">
                                ${this.renderScoreBar('Amount', bd.amount_score ?? 0)}
                                ${this.renderScoreBar('Reference', bd.reference_score ?? 0)}
                                ${this.renderScoreBar('Timing', bd.timing_score ?? 0)}
                                ${this.renderScoreBar('Entity', bd.entity_score ?? 0)}
                            </div>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');

        return `
            <table>
                <thead>
                    <tr>
                        <th>Invoice</th>
                        <th>Payment Ref</th>
                        <th>Confidence</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="reviews-tbody">${rows}</tbody>
            </table>
        `;
    },

    /* ── Reviews Table (from GET /api/reconcile/reviews) ────────────────── */

    renderReviewsTable(data) {
        if (!data || data.length === 0) {
            return this.renderEmptyState('No pending reviews', 'Run the reconciliation engine first. Items that need human review will appear here.');
        }

        const rows = data.map((item, i) => `
            <tr class="review-row" data-index="${i}">
                <td class="mono">#${item.review_id}</td>
                <td class="mono">INV-${item.invoice_id}</td>
                <td class="mono">PMT-${item.payment_id}</td>
                <td>${this.renderConfidenceBadge(item.weighted_confidence)}</td>
                <td><span class="badge warning">${item.review_status}</span></td>
            </tr>
            <tr class="review-details-row">
                <td colspan="5" style="padding: 0; border: none;">
                    <div class="review-details">
                        <div class="review-details-header">
                            <h4>Score Breakdown</h4>
                            <div class="review-actions">
                                <button class="btn btn-success review-action" data-review-id="${item.review_id}" data-action="MATCH">Match</button>
                                <button class="btn btn-secondary review-action" data-review-id="${item.review_id}" data-action="DECLINE">Decline</button>
                            </div>
                        </div>
                        <div class="data-comparison" style="margin-bottom: 24px; background: var(--bg-surface-hover); border-radius: var(--radius-md); padding: 12px; border: 1px solid var(--border);">
                            <h4 style="margin-bottom: 12px; font-size: 13px; color: var(--text-primary);">Data Comparison</h4>
                            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                                <thead>
                                    <tr style="text-align: left; border-bottom: 1px solid var(--border-strong);">
                                        <th style="padding: 8px; color: var(--text-secondary);">Field</th>
                                        <th style="padding: 8px; color: var(--text-secondary);">Invoice Data</th>
                                        <th style="padding: 8px; color: var(--text-secondary);">PSID Data</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr style="border-bottom: 1px solid var(--border);">
                                        <td style="padding: 8px; font-weight: 500;">Reference / No.</td>
                                        <td style="padding: 8px;" class="mono">${item.invoice_number || '-'}</td>
                                        <td style="padding: 8px;" class="mono">${item.payment_reference || '-'}</td>
                                    </tr>
                                    <tr style="border-bottom: 1px solid var(--border);">
                                        <td style="padding: 8px; font-weight: 500;">Amount</td>
                                        <td style="padding: 8px;" class="mono">${item.invoice_amount ? item.invoice_amount.toFixed(2) : '-'}</td>
                                        <td style="padding: 8px;" class="mono">${item.payment_amount ? item.payment_amount.toFixed(2) : '-'}</td>
                                    </tr>
                                    <tr style="border-bottom: 1px solid var(--border);">
                                        <td style="padding: 8px; font-weight: 500;">Date</td>
                                        <td style="padding: 8px;">${item.invoice_date || '-'}</td>
                                        <td style="padding: 8px;">${item.payment_date || '-'}</td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 8px; font-weight: 500;">Entity / Name</td>
                                        <td style="padding: 8px;">${item.buyer_name || '-'}</td>
                                        <td style="padding: 8px;">${item.sender_name || '-'}</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                        <h4 style="margin-bottom: 12px; font-size: 13px; color: var(--text-primary);">Score Breakdown</h4>
                        <div class="scores-grid">
                            ${this.renderScoreBar('Amount', item.amount_score)}
                            ${this.renderScoreBar('Reference', item.reference_score)}
                            ${this.renderScoreBar('Timing', item.timing_score)}
                            ${this.renderScoreBar('Entity', item.entity_score)}
                        </div>
                    </div>
                </td>
            </tr>
        `).join('');

        return `
            <table>
                <thead>
                    <tr>
                        <th>Review ID</th>
                        <th>Invoice</th>
                        <th>Payment</th>
                        <th>Confidence</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody id="reviews-tbody">${rows}</tbody>
            </table>
        `;
    },

    /* ── Dashboard View ────────────────────────────────────────────────── */

    renderDashboard(lastRunData) {
        if (!lastRunData) {
            return `
                <div class="page-header">
                    <h2>Dashboard</h2>
                    <p>Invoice reconciliation overview</p>
                </div>
                <div class="card">
                    ${this.renderEmptyState(
                        'No reconciliation data yet',
                        'Upload your data, then run reconciliation. Results will appear here.'
                    )}
                </div>
            `;
        }

        const s = lastRunData.summary;
        const total = lastRunData.payments_processed;
        const autoTotal = (s.auto_matched || 0) + (s.bulk_matched || 0);
        const yieldPct = total ? ((autoTotal / total) * 100).toFixed(1) : '0';

        return `
            <div class="page-header">
                <h2>Dashboard</h2>
                <p>Results from the most recent reconciliation run</p>
            </div>

            <div class="metrics-grid">
                ${this.renderMetricCard('Payments Processed', total)}
                ${this.renderMetricCard('Auto-Matched', autoTotal, `${yieldPct}% yield`, 'success')}
                ${this.renderMetricCard('Flagged for Review', s.flagged_for_review || 0)}
                ${this.renderMetricCard('Manually Matched', s.manually_matched || 0, '', 'info')}
                ${this.renderMetricCard('Discarded', s.discarded || 0)}
            </div>

            <div class="card">
                <div class="tabs">
                    <button class="tab-btn active" data-tab="auto">Auto Matched</button>
                    <button class="tab-btn" data-tab="bulk">Bulk Matched</button>
                    <button class="tab-btn" data-tab="flagged">Flagged</button>
                    <button class="tab-btn" data-tab="discarded">Discarded</button>
                </div>
                <div class="tab-content active" id="tab-auto">
                    ${this.renderAutoMatchTable(lastRunData.auto_matched)}
                </div>
                <div class="tab-content" id="tab-bulk">
                    ${this.renderBulkMatchTable(lastRunData.bulk_matched)}
                </div>
                <div class="tab-content" id="tab-flagged">
                    ${this.renderFlaggedTable(lastRunData.flagged_for_review)}
                </div>
                <div class="tab-content" id="tab-discarded">
                    ${this.renderDiscardedTable(lastRunData.discarded)}
                </div>
            </div>
        `;
    },

    /* ── Upload View ───────────────────────────────────────────────────── */

    renderUploadView() {
        return `
            <div class="page-header">
                <h2>Upload Data</h2>
                <p>Import invoices and payments from CSV files</p>
            </div>

            <div class="upload-grid">
                <div class="card">
                    <div class="card-header">Invoices CSV</div>
                    <div class="card-body">
                        <div class="upload-columns">
                            <div class="section-label">Required columns</div>
                            <div class="column-tags">
                                <span class="column-tag">invoice_number</span>
                                <span class="column-tag">company_code</span>
                                <span class="column-tag">buyer_business_name</span>
                                <span class="column-tag">issue_date</span>
                                <span class="column-tag">due_date</span>
                                <span class="column-tag">product_code</span>
                                <span class="column-tag">quantity</span>
                                <span class="column-tag">unit_price</span>
                                <span class="column-tag">sales_value</span>
                            </div>
                            <div class="section-label" style="margin-top:12px">Optional</div>
                            <div class="column-tags">
                                <span class="column-tag optional">buyer_iden_code</span>
                            </div>
                        </div>
                        <div class="upload-area" id="invoices-drop-zone">
                            <input type="file" id="file-invoices" accept=".csv" class="file-input">
                            <label for="file-invoices" class="file-label">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                    <polyline points="17 8 12 3 7 8"/>
                                    <line x1="12" y1="3" x2="12" y2="15"/>
                                </svg>
                                <span class="file-label-text">Choose CSV file</span>
                                <span class="file-label-hint">or drag and drop</span>
                            </label>
                            <div class="file-selected" id="invoices-file-name"></div>
                        </div>
                        <div style="display:flex;align-items:center;gap:12px;margin-top:16px;">
                            <button id="btn-upload-invoices" class="btn btn-primary" disabled>Upload Invoices</button>
                            <span class="upload-result" id="invoices-result"></span>
                        </div>
                    </div>
                </div>

                <div class="card">
                    <div class="card-header">Payments CSV</div>
                    <div class="card-body">
                        <div class="upload-columns">
                            <div class="section-label">Required columns</div>
                            <div class="column-tags">
                                <span class="column-tag">reference</span>
                                <span class="column-tag">amount</span>
                                <span class="column-tag">payment_date</span>
                                <span class="column-tag">sender_name</span>
                            </div>
                        </div>
                        <div class="upload-area" id="payments-drop-zone">
                            <input type="file" id="file-payments" accept=".csv" class="file-input">
                            <label for="file-payments" class="file-label">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                    <polyline points="17 8 12 3 7 8"/>
                                    <line x1="12" y1="3" x2="12" y2="15"/>
                                </svg>
                                <span class="file-label-text">Choose CSV file</span>
                                <span class="file-label-hint">or drag and drop</span>
                            </label>
                            <div class="file-selected" id="payments-file-name"></div>
                        </div>
                        <div style="display:flex;align-items:center;gap:12px;margin-top:16px;">
                            <button id="btn-upload-payments" class="btn btn-primary" disabled>Upload Payments</button>
                            <span class="upload-result" id="payments-result"></span>
                        </div>
                    </div>
                </div>
            </div>
        `;
    },

    /* ── Reconcile View (settings + execute) ───────────────────────────── */

    renderReconcileView() {
        return `
            <div class="page-header">
                <h2>Reconciliation</h2>
                <p>Configure scoring weights and thresholds, then execute</p>
            </div>

            <div class="card">
                <div class="card-header">Engine Configuration</div>
                <div class="card-body">
                    <div class="settings-grid">
                        <div>
                            <div class="section-label">Scoring Weights</div>
                            <div class="form-group">
                                <label>Amount</label>
                                <div class="range-with-value">
                                    <input type="range" id="weight-amount" min="0" max="1" step="0.01" value="0.45">
                                    <span id="val-amount">0.45</span>
                                </div>
                            </div>
                            <div class="form-group">
                                <label>Reference</label>
                                <div class="range-with-value">
                                    <input type="range" id="weight-reference" min="0" max="1" step="0.01" value="0.30">
                                    <span id="val-reference">0.30</span>
                                </div>
                            </div>
                            <div class="form-group">
                                <label>Timing</label>
                                <div class="range-with-value">
                                    <input type="range" id="weight-timing" min="0" max="1" step="0.01" value="0.15">
                                    <span id="val-timing">0.15</span>
                                </div>
                            </div>
                            <div class="form-group">
                                <label>Entity</label>
                                <div class="range-with-value">
                                    <input type="range" id="weight-entity" min="0" max="1" step="0.01" value="0.10">
                                    <span id="val-entity">0.10</span>
                                </div>
                            </div>

                            <div class="weight-sum-container">
                                <div class="weight-sum-header">
                                    <span>Weight Total</span>
                                    <span id="weight-total-val">1.00</span>
                                </div>
                                <div class="weight-sum-track">
                                    <div class="weight-segment ws-amount" id="seg-amount" style="width: 45%"></div>
                                    <div class="weight-segment ws-reference" id="seg-reference" style="width: 30%"></div>
                                    <div class="weight-segment ws-timing" id="seg-timing" style="width: 15%"></div>
                                    <div class="weight-segment ws-entity" id="seg-entity" style="width: 10%"></div>
                                </div>
                                <div class="weight-error" id="weight-error">Weights must sum exactly to 1.0</div>
                            </div>
                        </div>

                        <div>
                            <div class="section-label">Triage Thresholds</div>
                            <div class="form-group">
                                <label>Auto-Match Threshold</label>
                                <div class="description">Minimum WCI score for automatic matching</div>
                                <input type="number" class="form-control" id="thresh-auto" min="0" max="1" step="0.01" value="0.85" style="width: 120px;">
                            </div>
                            <div class="form-group">
                                <label>Review Threshold</label>
                                <div class="description">Minimum WCI score for manual review (below = discard)</div>
                                <input type="number" class="form-control" id="thresh-review" min="0" max="1" step="0.01" value="0.50" style="width: 120px;">
                            </div>
                        </div>
                    </div>

                    <div style="margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--border);">
                        <button id="btn-run" class="btn btn-primary">
                            Execute Reconciliation
                        </button>
                    </div>
                </div>
            </div>

            <div id="run-results"></div>
        `;
    },

    /* ── Reviews View (from GET endpoint) ──────────────────────────────── */

    renderReviewsView(reviews) {
        return `
            <div class="page-header">
                <h2>Manual Reviews</h2>
                <p>Invoice–payment pairs requiring human verification</p>
            </div>
            <div class="card">
                ${this.renderReviewsTable(reviews)}
            </div>
        `;
    }
};
