import os

import pytest
from fastapi.testclient import TestClient

from app.cua.chooser import gate_model_action
from app.cua.controller import Job, run_controller
from app.cua.memory_browser import MemoryBrowser
from app.cua.schemas import ComputerAction, PolicyError, validate_action
from app.cua.session import load_session
from app.cua.workbook_io import ledger_rows, reset_workbook, verify_ledger
from app.cua.worker import run_workbook_task
from app.documents import (
    AMBIGUOUS,
    BAD_PRICE,
    CLEAN,
    DUPLICATE,
    LOST_RESPONSE,
    MISMATCH,
    MISSING_PO,
    RETRY_CLEAN,
    TAX_ERROR,
    UNKNOWN_VENDOR,
    invoice_pdf,
    poor_scan_pdf,
)
from app.extractor import TemplateExtractor
from app.main import create_app

ORIGIN = "http://127.0.0.1:8765"
INSTRUCTION = "Process the latest invoice against the finance workbook and record only what the rules allow."


def _browser(path):
    return MemoryBrowser(path)


def _run(store, path, spec, key, *, pdf=None, filename=None, faults=False, reset=True, choice=None):
    browser = _browser(path)
    return run_workbook_task(
        store=store,
        extractor=TemplateExtractor(),
        workbook_path=path,
        browser_factory=lambda task_id: browser,
        origin=ORIGIN,
        instruction=INSTRUCTION,
        filename=filename or spec.filename,
        pdf_bytes=pdf if pdf is not None else invoice_pdf(spec),
        idempotency_key=key,
        faults_enabled=faults,
        reset=reset,
        vendor_choice=choice,
    )


def test_policy_rejects_unknown_sheet_status_and_timeout():
    with pytest.raises(PolicyError):
        validate_action(
            ComputerAction(goal="Open payroll", action="select_sheet", sheet="Payroll", expected_result="It opens"),
            origin=ORIGIN,
        )
    with pytest.raises(PolicyError):
        validate_action(
            ComputerAction(
                goal="Mark it posted",
                action="edit_cell",
                sheet="Invoice_Ledger",
                row=1,
                column="Status",
                value="PAID",
                expected_result="The status changes",
            ),
            origin=ORIGIN,
        )
    with pytest.raises(PolicyError):
        validate_action(
            ComputerAction(goal="Wait forever", action="save_workbook", expected_result="Saved", timeout_ms=999_999),
            origin=ORIGIN,
        )


def test_retry_budget_stops_a_save_that_never_persists(tmp_path):
    path = tmp_path / "Finance_AP_Ledger.xlsx"

    class AlwaysFails(MemoryBrowser):
        def _save(self):
            self.dialog = "save_error"
            self.save_state = "error"

    browser = AlwaysFails(path)
    job = Job(
        invoice_id="INV-2010",
        source_pdf="northwind-inv-2010.pdf",
        vendor_query="Northwind Supplies",
        vendor_expect="one",
        po_query="PO-1001",
        po_expect="one",
        grn_query="GRN-1001",
        grn_expect="one",
        intended={
            "Invoice ID": "INV-2010",
            "Vendor": "Northwind Supplies",
            "Tax ID": "27AABCU9603R1ZM",
            "Invoice Date": "2026-10-07",
            "PO ID": "PO-1001",
            "GRN ID": "GRN-1001",
            "Subtotal": "5000.00",
            "Tax Rate": "18",
            "Tax Amount": "900.00",
            "Total": "5900.00",
            "Status": "NEEDS_APPROVAL",
            "Exception": "",
            "Source PDF": "northwind-inv-2010.pdf",
            "Processed At": "now",
        },
    )
    browser.open(reset=True)
    progress = run_controller(browser, job, origin=ORIGIN, budget=2)
    assert progress.outcome == "FAILED"
    assert progress.save_attempts <= 3
    assert ledger_rows(path, "INV-2010") == []


def test_unexpected_dialog_is_closed_before_continuing(tmp_path, store):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    browser = MemoryBrowser(path)

    def factory(task_id):
        browser.open(reset=True)
        browser.plant_dialog("notice")
        original = browser.open

        def open_once(*args, **kwargs):
            if browser.opened and browser.dialog == "notice":
                return None
            return original(*args, **kwargs)

        browser.open = open_once
        return browser

    result = run_workbook_task(
        store=store,
        extractor=TemplateExtractor(),
        workbook_path=path,
        browser_factory=factory,
        origin=ORIGIN,
        instruction=INSTRUCTION,
        filename=CLEAN.filename,
        pdf_bytes=invoice_pdf(CLEAN),
        idempotency_key="dialog",
        reset=False,
    )
    assert "dismiss_dialog" in browser.actions
    assert result["verified"] is True


def test_clean_invoice_is_written_once_and_verified(tmp_path, store):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    result = _run(store, path, CLEAN, "clean")
    assert result["verified"] is True
    assert result["status"] == "completed"
    rows = ledger_rows(path, "INV-2001")
    assert len(rows) == 1
    assert rows[0]["Status"] == "NEEDS_APPROVAL"
    assert rows[0]["Total"] in {"5900.00", "5900.0"}
    assert rows[0]["Exception"] == ""
    again = _run(store, path, CLEAN, "clean-again", reset=False)
    assert again["verified"] is True
    assert len(ledger_rows(path, "INV-2001")) == 1


def test_quantity_tax_and_price_are_held(tmp_path, store):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    quantity = _run(store, path, MISMATCH, "qty")
    assert quantity["verified"] is True
    assert quantity["status"] == "needs_review"
    assert ledger_rows(path, "INV-2002")[0]["Exception"] == "QUANTITY_MISMATCH"
    assert ledger_rows(path, "INV-2002")[0]["Status"] == "NEEDS_REVIEW"

    tax = _run(store, path, TAX_ERROR, "tax", reset=False)
    assert tax["verified"] is True
    assert "TAX_MISMATCH" in ledger_rows(path, "INV-2100")[0]["Exception"]

    price = _run(store, path, BAD_PRICE, "price", reset=False)
    assert price["verified"] is True
    assert ledger_rows(path, "INV-2012")[0]["Exception"] == "PRICE_MISMATCH"
    assert ledger_rows(path, "INV-2012")[0]["Status"] == "NEEDS_REVIEW"


def test_duplicate_does_not_add_a_second_row(tmp_path, store):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    result = _run(store, path, DUPLICATE, "dup")
    assert result["verified"] is True
    rows = ledger_rows(path, "INV-3003")
    assert len(rows) == 1
    assert rows[0]["Vendor"] == "Apex Industrial"


def test_missing_po_is_held_with_exception_codes(tmp_path, store):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    result = _run(store, path, MISSING_PO, "missing")
    assert result["verified"] is True
    exception = ledger_rows(path, "INV-2200")[0]["Exception"]
    assert "PO_NOT_FOUND" in exception
    assert "GRN_NOT_FOUND" in exception


def test_unknown_vendor_and_unreadable_scan_do_not_post(tmp_path, store):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    unknown = _run(store, path, UNKNOWN_VENDOR, "unknown")
    assert unknown["verified"] is True
    assert ledger_rows(path, "INV-7001") == []
    scan = _run(store, path, None, "scan", pdf=poor_scan_pdf(), filename="scan-unreadable.pdf", reset=False)
    assert scan["verified"] is True
    assert scan["decision"] == "missing_information"
    checked = verify_ledger(path, invoice_id="", expect_row=False, source_pdf="scan-unreadable.pdf")
    assert checked["ok"] is True


def test_ambiguous_vendor_pauses_then_resumes(tmp_path, store):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    paused = _run(store, path, AMBIGUOUS, "helios")
    assert paused["status"] == "waiting_for_user"
    assert ledger_rows(path, "INV-5005") == []
    session = load_session(path, paused["id"])
    assert "Helios Metals" in session["candidates"]
    assert "Helios Metal Works" in session["candidates"]
    resumed = _run(store, path, AMBIGUOUS, "helios", reset=False, choice="Helios Metals")
    assert resumed["verified"] is True
    assert resumed["status"] == "completed"
    row = ledger_rows(path, "INV-5005")[0]
    assert row["Vendor"] == "Helios Metals"
    assert row["Status"] == "NEEDS_APPROVAL"
    assert len(ledger_rows(path, "INV-5005")) == 1


def test_save_failure_retries_and_lost_response_does_not_duplicate(tmp_path, store):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    saved = _run(store, path, RETRY_CLEAN, "fail-save", faults=True)
    assert saved["verified"] is True
    assert len(ledger_rows(path, "INV-2010")) == 1
    lost = _run(store, path, LOST_RESPONSE, "lost", faults=True, reset=False)
    assert lost["verified"] is True
    assert len(ledger_rows(path, "INV-2011")) == 1


def test_workbook_api_round_trip_and_tier1_still_posts(tmp_path):
    os.environ["CUA_FAULTS"] = "0"
    books = tmp_path / "Finance_AP_Ledger.xlsx"
    reset_workbook(books)
    app = create_app(tmp_path / "api.db", TemplateExtractor(), workbook_path=books)
    client = TestClient(app)
    loaded = client.get("/api/workbook")
    assert loaded.status_code == 200
    assert loaded.json()["sheets"]["Invoice_Ledger"][0][0] == "INV-3003"
    page = client.get("/workbook")
    assert page.status_code == 200
    assert "Finance workbook" in page.text
    tier1 = client.post(
        "/api/tasks",
        data={"instruction": INSTRUCTION, "idempotency_key": "tier1"},
        files={"file": ("northwind-inv-2001.pdf", invoice_pdf(CLEAN), "application/pdf")},
    )
    assert tier1.status_code == 200
    assert tier1.json()["verified"] is True
    assert ledger_rows(books, "INV-2001") == []


class _IllegalChooser:
    def __init__(self) -> None:
        self.failures = 0
        self.calls = 0

    @property
    def give_up(self) -> bool:
        return self.failures >= 2

    def note_failure(self, reason: str) -> None:
        del reason
        self.failures += 1

    def propose(self, job, obs, mismatch: str):
        del job, obs
        self.calls += 1
        if "Status" in mismatch or self.calls > 1:
            return ComputerAction(
                goal="Change the total",
                action="edit_cell",
                sheet="Invoice_Ledger",
                row=1,
                column="Total",
                value="1.00",
                expected_result="Total becomes 1.00",
            )
        return ComputerAction(
            goal="Mark it posted",
            action="edit_cell",
            sheet="Invoice_Ledger",
            row=1,
            column="Status",
            value="POSTED",
            expected_result="Status becomes POSTED",
        )


def test_model_cannot_type_status_or_amount_the_rules_did_not_allow(tmp_path):
    intended = {"Invoice ID": "INV-2001", "Status": "NEEDS_APPROVAL", "Total": "5900.00"}
    with pytest.raises(PolicyError, match="Status"):
        gate_model_action(
            ComputerAction(
                goal="Mark it posted",
                action="edit_cell",
                sheet="Invoice_Ledger",
                row=1,
                column="Status",
                value="POSTED",
                expected_result="Status becomes POSTED",
            ),
            intended,
        )
    with pytest.raises(PolicyError, match="Total"):
        gate_model_action(
            ComputerAction(
                goal="Change the total",
                action="edit_cell",
                sheet="Invoice_Ledger",
                row=1,
                column="Total",
                value="1.00",
                expected_result="Total becomes 1.00",
            ),
            intended,
        )
    job = Job(
        invoice_id="INV-2001",
        source_pdf="northwind-inv-2001.pdf",
        vendor_query="Northwind",
        vendor_expect="skip",
        po_query="",
        po_expect="none",
        grn_query="",
        grn_expect="none",
        intended=intended,
    )
    browser = MemoryBrowser(tmp_path / "book.xlsx")
    browser.open(None, reset=True)
    chooser = _IllegalChooser()
    seen: list[dict] = []
    progress = run_controller(browser, job, origin=ORIGIN, on_step=seen.append, chooser=chooser)
    assert chooser.give_up
    assert chooser.calls == 2
    assert progress.outcome == "READY_TO_VERIFY"
    assert all(record.action != "edit_cell" for record in progress.records)
    handoff = next(item for item in seen if item["phase"] == "handoff")
    opened = next(item for item in seen if item["phase"] == "about_to")
    assert handoff["goal"] == "Model action rejected twice. Switching to deterministic recovery. Financial rules remain enforced."
    assert opened["goal"] == "Opening the ledger."
    assert seen[seen.index(opened) + 1]["phase"] == "done"
    assert "POSTED" not in opened["goal"]
