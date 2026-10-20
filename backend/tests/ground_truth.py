"""Expected task outcomes.

The agent package never imports this module. Labels stay outside the model context.
"""

from __future__ import annotations

EXPECTATIONS: dict[str, dict] = {
    "clean": {
        "decision": "ready",
        "status": "completed",
        "persisted": True,
        "invoice_status": "pending_approval",
        "issues": [],
        "total": "5900.00",
    },
    "quantity_mismatch": {
        "decision": "needs_review",
        "status": "needs_review",
        "persisted": True,
        "invoice_status": "needs_review",
        "issues": ["quantity_mismatch"],
        "total": "1180.00",
    },
    "duplicate": {
        "decision": "needs_review",
        "status": "needs_review",
        "persisted": False,
        "invoice_status": None,
        "issues": ["duplicate_invoice"],
        "total": None,
    },
    "missing_po": {
        "decision": "missing_information",
        "status": "missing_information",
        "persisted": True,
        "invoice_status": "needs_review",
        "issues": ["po_missing", "grn_missing"],
        "total": "5900.00",
    },
    "tax_arithmetic": {
        "decision": "needs_review",
        "status": "needs_review",
        "persisted": True,
        "invoice_status": "needs_review",
        "issues": ["tax_arithmetic"],
        "total": "5100.00",
    },
    "ambiguous_vendor": {
        "decision": "needs_review",
        "status": "needs_review",
        "persisted": False,
        "invoice_status": None,
        "issues": ["vendor_ambiguous"],
        "total": None,
    },
    "unknown_vendor": {
        "decision": "needs_review",
        "status": "needs_review",
        "persisted": False,
        "invoice_status": None,
        "issues": ["vendor_unknown", "po_missing", "grn_missing"],
        "total": None,
    },
    "poor_scan": {
        "decision": "missing_information",
        "status": "missing_information",
        "persisted": False,
        "invoice_status": None,
        "issues": ["poor_scan"],
        "total": None,
    },
    "bad_schema": {
        "decision": "missing_information",
        "status": "missing_information",
        "persisted": False,
        "invoice_status": None,
        "issues": ["missing_required_fields"],
        "total": None,
    },
    "bad_price": {
        "decision": "needs_review",
        "status": "needs_review",
        "persisted": True,
        "invoice_status": "needs_review",
        "issues": ["price_mismatch"],
        "total": "117882.00",
    },
}
