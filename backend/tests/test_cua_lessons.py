"""The Groq system prompt learns the deterministic procedure from finished invoices."""

from pathlib import Path

from app.cua.lessons import (
    LEARNED_LIMIT,
    format_lesson,
    load_seed,
    remember_lesson,
    signature,
    steps_from_audit,
    system_prompt,
)
from app.cua.sql_ledger import SqlLedgerBrowser
from app.cua.worker import run_workbook_task
from app.documents import CLEAN, MISMATCH, invoice_pdf
from app.extractor import TemplateExtractor


def _run(store, tmp_path: Path, spec, key: str) -> dict:
    browser = SqlLedgerBrowser(store.conn)
    return run_workbook_task(
        store=store,
        extractor=TemplateExtractor(),
        workbook_path=tmp_path / "session.xlsx",
        browser_factory=lambda task_id: browser,
        origin="http://127.0.0.1:8765",
        instruction="Record this invoice on the ledger.",
        filename=spec.filename,
        pdf_bytes=invoice_pdf(spec),
        idempotency_key=key,
    )


def test_two_deterministic_invoices_match_the_prompt_examples(store, tmp_path):
    seed = {item["invoice_id"]: item for item in load_seed()}
    assert set(seed) == {"INV-2001", "INV-2002"}
    for spec, key in ((CLEAN, "lesson-clean"), (MISMATCH, "lesson-held")):
        result = _run(store, tmp_path, spec, key)
        assert result["verified"] is True
        fresh = steps_from_audit(store.audit_for(result["id"]))
        assert signature(fresh) == signature(seed[spec.invoice_number]["steps"])
        assert result["status"] == seed[spec.invoice_number]["outcome"]
    prompt = system_prompt()
    assert "INV-2001" in prompt
    assert "NEEDS_APPROVAL" in prompt
    assert "INV-2002" in prompt
    assert "QUANTITY_MISMATCH" in prompt
    assert "Do not add a second blank row." in prompt
    assert prompt.count("add_row") == 2


def test_a_new_invoice_is_added_and_the_window_keeps_the_seeds(store):
    remember_lesson(
        store.conn,
        "INV-2100",
        "needs_review",
        [
            {
                "step": "cua_act",
                "outcome": "ok",
                "detail": {
                    "goal": "Enter Status",
                    "action": "edit_cell",
                    "sheet": "Invoice_Ledger",
                    "column": "Status",
                    "value": "NEEDS_REVIEW",
                },
            }
        ],
    )
    prompt = system_prompt(
        [
            {"invoice_id": "INV-2001", "outcome": "completed", "steps": [{"action": "add_row", "goal": "duplicate seed"}]},
            *[
                {
                    "invoice_id": f"INV-{7000 + index}",
                    "outcome": "completed",
                    "steps": [{"goal": "Enter Invoice ID", "action": "edit_cell", "column": "Invoice ID", "value": f"INV-{7000 + index}"}],
                }
                for index in range(LEARNED_LIMIT + 1)
            ],
        ]
    )
    learned = system_prompt(
        [
            {
                "invoice_id": "INV-2100",
                "outcome": "needs_review",
                "steps": [{"goal": "Enter Status", "action": "edit_cell", "column": "Status", "value": "NEEDS_REVIEW"}],
            }
        ]
    )
    assert "INV-2100" in learned
    assert "finished as needs_review" in learned
    assert "INV-2001" in prompt
    assert "INV-2002" in prompt
    assert "INV-7000" not in prompt
    assert f"INV-{7000 + LEARNED_LIMIT}" in prompt
    assert "duplicate seed" not in prompt
    assert "Enter Status" in format_lesson(
        {"invoice_id": "INV-2100", "outcome": "needs_review", "steps": [{"goal": "Enter Status", "action": "edit_cell"}]}
    )
