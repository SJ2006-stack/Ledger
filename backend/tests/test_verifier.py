from app.documents import CLEAN, invoice_pdf
from app.extractor import TemplateExtractor
from app.orchestrator import Orchestrator
from app.verifier import Intention, verify


def test_verifier_reads_the_database_and_catches_a_tampered_total(store):
    orchestrator = Orchestrator(store, TemplateExtractor())
    result = orchestrator.run(
        instruction="Post this invoice if it matches the purchase order and goods receipt.",
        filename=CLEAN.filename,
        pdf_bytes=invoice_pdf(CLEAN),
        idempotency_key="verify-clean",
    )
    assert result["status"] == "completed"
    assert result["verified"] is True
    invoice_id = result["invoice_id"]
    store.tamper_total(invoice_id, 1)
    checked = verify(
        store,
        Intention(
            idempotency_key="verify-clean",
            expect_row=True,
            vendor_id=1,
            invoice_number="INV-2001",
            po_number="PO-1001",
            status="pending_approval",
            subtotal_cents=500000,
            tax_cents=90000,
            total_cents=590000,
        ),
    )
    assert checked.ok is False
    assert "total_cents" in checked.mismatches
