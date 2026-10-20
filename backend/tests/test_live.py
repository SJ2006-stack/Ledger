from decimal import Decimal

import pytest

from app.accounting import AccountingStore
from app.config import Settings
from app.db import connect, init_db
from app.documents import CLEAN, MISMATCH, invoice_pdf
from app.extractor import GeminiExtractor
from app.orchestrator import Orchestrator
from app.seed import seed

INSTRUCTION = (
    "Read the uploaded invoice, match it to the purchase order and goods receipt, "
    "and post it for approval only when the vendor, quantities, prices, and totals agree."
)


@pytest.mark.live
def test_groq_clean_and_mismatch(tmp_path):
    settings = Settings.from_env()
    assert settings.groq_api_key
    assert settings.model == "qwen/qwen3.8-27b"
    conn = connect(tmp_path / "live.db")
    init_db(conn)
    seed(conn)
    store = AccountingStore(conn)
    orchestrator = Orchestrator(store, GeminiExtractor(settings.groq_api_key, settings.model))

    clean = orchestrator.run(
        instruction=INSTRUCTION,
        filename=CLEAN.filename,
        pdf_bytes=invoice_pdf(CLEAN),
        idempotency_key="live-clean",
    )
    assert clean["status"] == "completed", clean["explanation"]
    assert clean["verified"] is True
    assert _snapshot(clean)["model"] == "qwen/qwen3.8-27b"
    assert _field_hits(_snapshot(clean)["snapshot"]) == 8
    invoice = store.get_invoice(clean["invoice_id"])
    assert invoice["status"] == "pending_approval"
    assert invoice["total"] == "5900.00"
    assert invoice["invoice_number"] == "INV-2001"

    mismatch = orchestrator.run(
        instruction=INSTRUCTION,
        filename=MISMATCH.filename,
        pdf_bytes=invoice_pdf(MISMATCH),
        idempotency_key="live-mismatch",
    )
    assert mismatch["status"] == "needs_review", mismatch["explanation"]
    assert mismatch["decision"] == "needs_review"
    assert "quantity_mismatch" in mismatch["issues"]
    assert mismatch["status"] != "completed"
    held = store.get_invoice(mismatch["invoice_id"])
    assert held["status"] == "needs_review"


def _snapshot(result: dict) -> dict:
    event = next(
        item for item in result["audit"] if item["step"] == "extract_fields" and item["outcome"] == "extracted"
    )
    return event["detail"]


def _field_hits(snapshot: dict) -> int:
    expected = {
        "vendor_name": "Northwind Supplies",
        "invoice_number": "INV-2001",
        "po_number": "PO-1001",
        "grn_number": "GRN-1001",
        "quantity": Decimal("100"),
        "unit_price": Decimal("50.00"),
        "tax_amount": Decimal("900.00"),
        "total": Decimal("5900.00"),
    }
    hits = 0
    for key, value in expected.items():
        raw = snapshot[key]
        if isinstance(value, Decimal):
            if raw is not None and Decimal(str(raw)) == value:
                hits += 1
        elif raw == value:
            hits += 1
    return hits
