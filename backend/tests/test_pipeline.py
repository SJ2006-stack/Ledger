import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.documents import (
    AMBIGUOUS,
    BAD_PRICE,
    CLEAN,
    DUPLICATE,
    LOST_RESPONSE,
    MISMATCH,
    MISSING_PO,
    POOR_SCAN_NAME,
    RETRY_CLEAN,
    TAX_ERROR,
    UNKNOWN_VENDOR,
    invoice_pdf,
    poor_scan_pdf,
)
from app.extractor import TemplateExtractor
from app.orchestrator import Orchestrator
from app.parser import parse_pdf
from tests.ground_truth import EXPECTATIONS

INSTRUCTION = (
    "Read the uploaded invoice, match it to the purchase order and goods receipt, "
    "and post it for approval only when the vendor, quantities, prices, and totals agree."
)

DOCUMENTS = {
    "clean": CLEAN,
    "quantity_mismatch": MISMATCH,
    "duplicate": DUPLICATE,
    "missing_po": MISSING_PO,
    "tax_arithmetic": TAX_ERROR,
    "ambiguous_vendor": AMBIGUOUS,
    "unknown_vendor": UNKNOWN_VENDOR,
    "bad_price": BAD_PRICE,
}


class BlankQuantity(TemplateExtractor):
    def extract(self, document_text: str, filename: str):
        base = super().extract(document_text, filename)
        line = replace(base.line_items[0], quantity=None)
        return replace(base, line_items=(line,))


class ForcePost(TemplateExtractor):
    def extract(self, document_text: str, filename: str):
        return replace(super().extract(document_text, filename), proposed_action="post_invoice")


def _run(store, spec, key: str, extractor=None, pdf: bytes | None = None, lose: bool = False):
    orchestrator = Orchestrator(store, extractor or TemplateExtractor())
    payload = pdf if pdf is not None else invoice_pdf(spec)
    return orchestrator.run(
        instruction=INSTRUCTION,
        filename=spec.filename if spec is not None else POOR_SCAN_NAME,
        pdf_bytes=payload,
        idempotency_key=key,
        lose_response_after_save=lose,
    )


def test_scoreboard(store, capsys):
    results = {}
    field_hits = 0
    field_total = 0
    for name, spec in DOCUMENTS.items():
        parsed = parse_pdf(invoice_pdf(spec))
        extracted = TemplateExtractor().extract(parsed.text, spec.filename)
        hits, total = _score_extraction(extracted, spec)
        field_hits += hits
        field_total += total
        results[name] = _run(store, spec, f"case-{name}")

    results["poor_scan"] = _run(store, None, "case-poor", pdf=poor_scan_pdf())
    results["bad_schema"] = _run(store, CLEAN, "case-schema", extractor=BlankQuantity())

    decisions_ok = 0
    executions_ok = 0
    verifications_ok = 0
    for name, expected in EXPECTATIONS.items():
        result = results[name]
        assert result["decision"] == expected["decision"], name
        assert result["status"] == expected["status"], name
        assert set(result["issues"]) == set(expected["issues"]), (name, result["issues"])
        assert result["status"] != "completed" or expected["decision"] == "ready"
        decisions_ok += 1
        if expected["persisted"]:
            invoice = store.get_invoice(result["invoice_id"])
            assert invoice is not None, name
            assert invoice["status"] == expected["invoice_status"], name
            assert invoice["total"] == expected["total"], name
            assert invoice["status"] != "approved"
            executions_ok += 1
        else:
            assert result["invoice_id"] is None, name
            executions_ok += 1
        assert result["verified"] is True, name
        verifications_ok += 1

    retry_key = "case-save-retry"
    store.fail_next = 1
    failed = _run(store, RETRY_CLEAN, retry_key)
    assert failed["status"] == "failed"
    assert store.count_with_number("INV-2010") == 0
    recovered = _run(store, RETRY_CLEAN, retry_key)
    assert recovered["status"] == "completed"
    assert store.count_with_number("INV-2010") == 1

    lost_key = "case-lost"
    lost = _run(store, LOST_RESPONSE, lost_key, lose=True)
    assert lost["status"] == "failed"
    assert lost["invoice_id"] is None
    assert store.count_with_number("INV-2011") == 1
    found = _run(store, LOST_RESPONSE, lost_key)
    assert found["status"] == "completed"
    assert store.count_with_number("INV-2011") == 1
    assert found["invoice_id"] is not None

    false_success = 0
    for task in store.list_tasks():
        if task["status"] != "completed":
            continue
        invoice = store.get_invoice(task["invoice_id"]) if task["invoice_id"] else None
        if task["decision"] != "ready" or not task["verified"] or invoice is None:
            false_success += 1
        elif invoice["status"] != "pending_approval":
            false_success += 1

    metrics = {
        "extraction_accuracy": field_hits / field_total,
        "decision_accuracy": decisions_ok / len(EXPECTATIONS),
        "execution_success": executions_ok / len(EXPECTATIONS),
        "verification_accuracy": verifications_ok / len(EXPECTATIONS),
        "false_success": false_success,
        "cases": len(EXPECTATIONS) + 2,
    }
    print("METRICS " + json.dumps(metrics))
    captured = capsys.readouterr()
    assert "METRICS" in captured.out
    assert metrics["false_success"] == 0
    assert metrics["extraction_accuracy"] == 1
    assert metrics["decision_accuracy"] == 1


class Noted(TemplateExtractor):
    def extract(self, document_text: str, filename: str):
        base = super().extract(document_text, filename)
        return replace(
            base,
            thinking="The tax line is printed as 900.",
            thoughts_tokens=40,
            model_note="The printed totals agree.",
        )


def test_thinking_is_recorded_on_the_extract_step(store):
    result = _run(store, CLEAN, "thinking", extractor=Noted())
    event = next(
        item for item in result["audit"] if item["step"] == "extract_fields" and item["outcome"] == "extracted"
    )
    assert event["detail"]["thinking"] == "The tax line is printed as 900."
    assert event["detail"]["thoughts_tokens"] == 40
    assert event["detail"]["model_note"] == "The printed totals agree."


def test_thought_text_keeps_only_thought_parts():
    from types import SimpleNamespace

    from app.extractor import _thought_text

    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(thought=True, text="  Check the tax line. "),
                        SimpleNamespace(thought=False, text='{"total": 1}'),
                        SimpleNamespace(thought=True, text="   "),
                        SimpleNamespace(thought=True, text="Then compare the receipt."),
                    ]
                )
            )
        ]
    )
    assert _thought_text(response) == "Check the tax line.\n\nThen compare the receipt."


def test_model_cannot_force_approval(store):
    result = _run(store, MISMATCH, "override", extractor=ForcePost())
    assert result["status"] == "needs_review"
    assert result["final_action"] == "hold_for_review"
    assert result["model_action"] == "post_invoice"
    decide = next(event for event in result["audit"] if event["step"] == "decide")
    assert decide["detail"]["overridden"] is True


def test_labels_stay_out_of_the_agent():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    source = "\n".join(path.read_text() for path in app_dir.glob("*.py"))
    assert "ground_truth" not in source
    assert "EXPECTATIONS" not in source


def _score_extraction(extracted, spec) -> tuple[int, int]:
    line = spec.lines[0]
    expected = {
        "vendor_name": spec.vendor_name,
        "invoice_number": spec.invoice_number,
        "po_number": spec.po_number or None,
        "grn_number": spec.grn_number or None,
        "quantity": Decimal(line.quantity),
        "unit_price": Decimal(line.unit_price),
        "tax_amount": Decimal(spec.tax_amount),
        "total": Decimal(spec.total),
    }
    actual_line = extracted.line_items[0]
    actual = {
        "vendor_name": extracted.vendor_name,
        "invoice_number": extracted.invoice_number,
        "po_number": extracted.po_number,
        "grn_number": extracted.grn_number,
        "quantity": actual_line.quantity,
        "unit_price": actual_line.unit_price,
        "tax_amount": extracted.tax_amount,
        "total": extracted.total,
    }
    hits = sum(1 for key, value in expected.items() if actual[key] == value)
    return hits, len(expected)
