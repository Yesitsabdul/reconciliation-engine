/* ═══════════════════════════════════════════════════════════════════════════
   Application Controller — Reconciliation Engine

   Handles routing, state management, event binding, and API orchestration.
   ═══════════════════════════════════════════════════════════════════════════ */

const state = {
    lastRunData: null,
    reviews: null,
    currentView: 'dashboard'
};

/* ── Clock ─────────────────────────────────────────────────────────────── */

function updateTimestamp() {
    const el = document.getElementById('system-timestamp');
    if (el) {
        el.textContent = new Date().toLocaleTimeString([], {
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
    }
}
setInterval(updateTimestamp, 1000);
updateTimestamp();

/* ── Health Polling ────────────────────────────────────────────────────── */

async function pollHealth() {
    const indicator = document.getElementById('health-indicator');
    if (!indicator) return;
    const dot  = indicator.querySelector('.status-dot');
    const text = indicator.querySelector('.status-text');

    try {
        await api.checkHealth();
        dot.className  = 'status-dot online';
        text.textContent = 'Online';
    } catch {
        dot.className  = 'status-dot offline';
        text.textContent = 'Offline';
    }
}
setInterval(pollHealth, 30000);
pollHealth();

/* ── Toast Notifications ──────────────────────────────────────────────── */

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        setTimeout(() => toast.remove(), 250);
    }, 3500);
}

/* ═══════════════════════════════════════════════════════════════════════════
   Router
   ═══════════════════════════════════════════════════════════════════════════ */

async function renderView() {
    const main = document.getElementById('main-content');
    if (!main) return;

    const hash = window.location.hash.slice(1) || 'dashboard';

    // Update nav active state
    document.querySelectorAll('.nav-item').forEach(el => {
        el.classList.toggle('active', el.getAttribute('data-target') === hash);
    });

    state.currentView = hash;

    switch (hash) {
        case 'dashboard':
            main.innerHTML = components.renderDashboard(state.lastRunData);
            bindTabs();
            bindReviewToggles();
            break;

        case 'upload':
            main.innerHTML = components.renderUploadView();
            bindUploadUI();
            break;

        case 'reconcile':
            main.innerHTML = components.renderReconcileView();
            bindConfigUI();
            document.getElementById('btn-run')
                ?.addEventListener('click', handleRunReconciliation);

            if (state.lastRunData) {
                const resultsEl = document.getElementById('run-results');
                if (resultsEl) {
                    resultsEl.innerHTML = components.renderDashboard(state.lastRunData);
                    bindTabs();
                    bindReviewToggles();
                }
            }
            break;

        case 'reviews':
            main.innerHTML = `
                <div class="page-header">
                    <h2>Manual Reviews</h2>
                    <p>Invoice–payment pairs requiring human verification</p>
                </div>
                <div class="card">
                    ${components.renderEmptyState('Loading…', 'Fetching pending reviews from the server.')}
                </div>
            `;
            try {
                const reviews = await api.fetchReviews();
                state.reviews = reviews;
                main.innerHTML = components.renderReviewsView(reviews);
                bindReviewToggles();
            } catch (error) {
                main.innerHTML = `
                    <div class="page-header">
                        <h2>Manual Reviews</h2>
                        <p>Invoice–payment pairs requiring human verification</p>
                    </div>
                    <div class="card">
                        ${components.renderEmptyState(
                            'Failed to load reviews',
                            'Make sure the backend is running on port 8000 and try again.'
                        )}
                    </div>
                `;
                showToast('Failed to fetch reviews', 'error');
            }
            break;

        default:
            main.innerHTML = components.renderEmptyState('Page not found');
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Event Binding Helpers
   ═══════════════════════════════════════════════════════════════════════════ */

function bindTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const parent = e.target.closest('.card');
            if (!parent) return;

            parent.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            parent.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

            e.target.classList.add('active');
            const target = parent.querySelector(`#tab-${e.target.getAttribute('data-tab')}`);
            if (target) target.classList.add('active');
        });
    });
}

function bindReviewToggles() {
    document.querySelectorAll('.review-action').forEach(button => {
        button.addEventListener('click', async (e) => {
            e.stopPropagation();
            const action = button.getAttribute('data-action');
            const reviewId = button.getAttribute('data-review-id');
            if (!action || !reviewId) return;

            try {
                await api.decideReview(reviewId, action);
                showToast(`Review #${reviewId} ${action === 'MATCH' ? 'matched' : 'declined'}`);

                const summaryRow = button.closest('tr.review-row');
                if (summaryRow) {
                    const detailsRow = summaryRow.nextElementSibling;
                    summaryRow.remove();
                    if (detailsRow?.classList.contains('review-details-row')) {
                        detailsRow.remove();
                    }
                }

                // Update state.lastRunData so Dashboard reflects the cleared review
                if (state.lastRunData && state.lastRunData.flagged_for_review) {
                    const idx = state.lastRunData.flagged_for_review.findIndex(r => r.review_id == reviewId);
                    if (idx > -1) {
                        state.lastRunData.flagged_for_review.splice(idx, 1);
                        if (state.lastRunData.summary && state.lastRunData.summary.flagged_for_review > 0) {
                            state.lastRunData.summary.flagged_for_review--;
                            if (action === 'MATCH') {
                                state.lastRunData.summary.manually_matched = (state.lastRunData.summary.manually_matched || 0) + 1;
                            }
                        }
                    }
                }

                if (state.currentView === 'reviews') {
                    renderView();
                }
            } catch (error) {
                showToast(`Failed to ${action.toLowerCase()} review #${reviewId}`, 'error');
                console.error('Review decision error:', error);
            }
        });
    });

    document.querySelectorAll('.review-row').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.review-action')) return;
            row.classList.toggle('expanded');
        });
    });
}

function bindConfigUI() {
    const keys = ['amount', 'reference', 'timing', 'entity'];

    const updateSum = () => {
        let sum = 0;
        keys.forEach(key => {
            const val = parseFloat(document.getElementById(`weight-${key}`)?.value || 0);
            const display = document.getElementById(`val-${key}`);
            const seg = document.getElementById(`seg-${key}`);
            if (display) display.textContent = val.toFixed(2);
            if (seg) seg.style.width = `${val * 100}%`;
            sum += val;
        });

        const sumEl = document.getElementById('weight-total-val');
        const errorEl = document.getElementById('weight-error');
        const btn = document.getElementById('btn-run');
        const valid = Math.abs(sum - 1.0) <= 0.001;

        if (sumEl) {
            sumEl.textContent = sum.toFixed(2);
            sumEl.style.color = valid ? '' : 'var(--danger-text)';
        }
        if (errorEl) errorEl.classList.toggle('active', !valid);
        if (btn) btn.disabled = !valid;
    };

    keys.forEach(key => {
        document.getElementById(`weight-${key}`)
            ?.addEventListener('input', updateSum);
    });
}

/* ═══════════════════════════════════════════════════════════════════════════
   Upload Handlers
   ═══════════════════════════════════════════════════════════════════════════ */

function bindUploadUI() {
    // Wire up file inputs
    bindFileInput('file-invoices', 'invoices-file-name', 'btn-upload-invoices');
    bindFileInput('file-payments', 'payments-file-name', 'btn-upload-payments');

    // Wire up drag-and-drop
    bindDropZone('invoices-drop-zone', 'file-invoices', 'invoices-file-name', 'btn-upload-invoices');
    bindDropZone('payments-drop-zone', 'file-payments', 'payments-file-name', 'btn-upload-payments');

    // Upload buttons
    document.getElementById('btn-upload-invoices')
        ?.addEventListener('click', () => handleUpload('invoices'));
    document.getElementById('btn-upload-payments')
        ?.addEventListener('click', () => handleUpload('payments'));
}

function bindFileInput(inputId, nameId, btnId) {
    const input = document.getElementById(inputId);
    const nameEl = document.getElementById(nameId);
    const btn = document.getElementById(btnId);
    if (!input) return;

    input.addEventListener('change', () => {
        if (input.files.length > 0) {
            nameEl.textContent = input.files[0].name;
            nameEl.classList.add('active');
            btn.disabled = false;
        } else {
            nameEl.textContent = '';
            nameEl.classList.remove('active');
            btn.disabled = true;
        }
    });
}

function bindDropZone(zoneId, inputId, nameId, btnId) {
    const zone = document.getElementById(zoneId);
    const input = document.getElementById(inputId);
    const nameEl = document.getElementById(nameId);
    const btn = document.getElementById(btnId);
    if (!zone || !input) return;

    ['dragenter', 'dragover'].forEach(evt => {
        zone.addEventListener(evt, (e) => {
            e.preventDefault();
            zone.classList.add('drag-over');
        });
    });

    ['dragleave', 'drop'].forEach(evt => {
        zone.addEventListener(evt, (e) => {
            e.preventDefault();
            zone.classList.remove('drag-over');
        });
    });

    zone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0 && files[0].name.endsWith('.csv')) {
            input.files = files;
            nameEl.textContent = files[0].name;
            nameEl.classList.add('active');
            btn.disabled = false;
        }
    });
}

async function handleUpload(type) {
    const inputId = type === 'invoices' ? 'file-invoices' : 'file-payments';
    const btnId = type === 'invoices' ? 'btn-upload-invoices' : 'btn-upload-payments';
    const resultId = type === 'invoices' ? 'invoices-result' : 'payments-result';

    const input = document.getElementById(inputId);
    const btn = document.getElementById(btnId);
    const resultEl = document.getElementById(resultId);

    if (!input?.files?.length) return;

    const file = input.files[0];
    const originalHTML = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> Uploading…';
    btn.disabled = true;
    resultEl.textContent = '';
    resultEl.className = 'upload-result';

    try {
        const result = type === 'invoices'
            ? await api.uploadInvoices(file)
            : await api.uploadPayments(file);

        let msg = '';
        if (type === 'invoices') {
            msg = `${result.invoices_created} invoice(s), ${result.detail_rows_created} line item(s) imported`;
        } else {
            msg = `${result.payments_created} payment(s) imported`;
        }

        resultEl.textContent = '✓ ' + msg;
        resultEl.classList.add('success');
        showToast(msg);
    } catch (error) {
        resultEl.textContent = '✗ Upload failed';
        resultEl.classList.add('error');
        showToast(`Failed to upload ${type}`, 'error');
        console.error('Upload error:', error);
    } finally {
        btn.innerHTML = originalHTML;
        btn.disabled = false;
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Run Reconciliation
   ═══════════════════════════════════════════════════════════════════════════ */

async function handleRunReconciliation() {
    const btn = document.getElementById('btn-run');
    if (!btn) return;

    const config = {
        amount_weight:        parseFloat(document.getElementById('weight-amount')?.value ?? 0.45),
        reference_weight:     parseFloat(document.getElementById('weight-reference')?.value ?? 0.30),
        timing_weight:        parseFloat(document.getElementById('weight-timing')?.value ?? 0.15),
        entity_weight:        parseFloat(document.getElementById('weight-entity')?.value ?? 0.10),
        auto_match_threshold: parseFloat(document.getElementById('thresh-auto')?.value ?? 0.85),
        review_threshold:     parseFloat(document.getElementById('thresh-review')?.value ?? 0.50)
    };

    const originalHTML = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> Running…';
    btn.disabled = true;

    try {
        const result = await api.runReconciliation(config);
        state.lastRunData = result;
        showToast(`Reconciliation complete — ${result.payments_processed} payments processed`);

        const resultsEl = document.getElementById('run-results');
        if (resultsEl) {
            resultsEl.innerHTML = components.renderDashboard(result);
            bindTabs();
            bindReviewToggles();
        }
    } catch (error) {
        showToast('Reconciliation failed — check backend logs', 'error');
        console.error('Reconciliation error:', error);
    } finally {
        btn.innerHTML = originalHTML;
        btn.disabled = false;
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Init
   ═══════════════════════════════════════════════════════════════════════════ */

window.addEventListener('hashchange', renderView);
renderView();
