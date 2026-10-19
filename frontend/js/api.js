/* ═══════════════════════════════════════════════════════════════════════════
   API Client — Reconciliation Engine
   ═══════════════════════════════════════════════════════════════════════════ */

const API_BASE = (window.location.hostname === 'localhost' && window.location.port !== '8000') || window.location.protocol === 'file:'
    ? 'http://localhost:8000'
    : '';

async function apiCall(endpoint, options = {}) {
    const response = await fetch(`${API_BASE}${endpoint}`, {
        ...options,
        headers: {
            'Content-Type': 'application/json',
            ...options.headers
        }
    });
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`API ${response.status}: ${errorText}`);
    }
    return await response.json();
}

async function apiUpload(endpoint, file) {
    const formData = new FormData();
    formData.append('file', file);
    const response = await fetch(`${API_BASE}${endpoint}`, {
        method: 'POST',
        body: formData
    });
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Upload failed (${response.status}): ${errorText}`);
    }
    return await response.json();
}

const api = {
    checkHealth: () => apiCall('/health'),

    runReconciliation: (config) => apiCall('/api/reconcile/run', {
        method: 'POST',
        body: JSON.stringify(config)
    }),

    fetchReviews: () => apiCall('/api/reconcile/reviews'),

    decideReview: (reviewId, action) => apiCall(
        `/api/reconcile/reviews/${reviewId}/decision`,
        {
            method: 'POST',
            body: JSON.stringify({ action })
        }
    ),

    uploadInvoices: (file) => apiUpload('/api/upload/invoices', file),

    uploadPayments: (file) => apiUpload('/api/upload/payments', file)
};
