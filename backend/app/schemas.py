from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Decision(str, Enum):
    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    MISSING_INFORMATION = "missing_information"


class Issue(str, Enum):
    VENDOR_AMBIGUOUS = "vendor_ambiguous"
    VENDOR_UNKNOWN = "vendor_unknown"
    VENDOR_PO_CONFLICT = "vendor_po_conflict"
    PO_MISSING = "po_missing"
    GRN_MISSING = "grn_missing"
    DUPLICATE_INVOICE = "duplicate_invoice"
    QUANTITY_MISMATCH = "quantity_mismatch"
    PRICE_MISMATCH = "price_mismatch"
    TAX_ARITHMETIC = "tax_arithmetic"
    LINE_UNMATCHED = "line_unmatched"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    POOR_SCAN = "poor_scan"


class Action(str, Enum):
    POST_INVOICE = "post_invoice"
    HOLD_FOR_REVIEW = "hold_for_review"
    REQUEST_CLARIFICATION = "request_clarification"


class AuditEventOut(BaseModel):
    step: str
    outcome: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class TaskOut(BaseModel):
    id: str
    instruction: str
    filename: str | None
    status: str
    decision: str | None
    explanation: str
    invoice_id: int | None
    verified: bool
    issues: list[str]
    model_action: str | None
    final_action: str | None
    audit: list[AuditEventOut]
    usage: dict[str, Any] | None = None
