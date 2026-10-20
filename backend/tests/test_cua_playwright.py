import os
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn

from app.cua.playwright_browser import PlaywrightBrowser
from app.cua.schemas import ComputerAction
from app.cua.workbook_io import ledger_rows, reset_workbook
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

INSTRUCTION = "Process this invoice in the finance workbook and record only what the rules allow."


def _port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def cua_server(tmp_path_factory):
    os.environ["CUA_FAULTS"] = "1"
    root = tmp_path_factory.mktemp("cua-server")
    books = root / "Finance_AP_Ledger.xlsx"
    reset_workbook(books)
    port = _port()
    app = create_app(root / "books.db", TemplateExtractor(), workbook_path=books)
    origin = f"http://127.0.0.1:{port}"
    app.state.cua_origin = origin
    thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "127.0.0.1", "port": port, "log_level": "warning"},
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(origin + "/api/health", timeout=1) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("The workbook server did not start.")
    return {"origin": origin, "books": books, "app": app}


def test_playwright_can_filter_edit_save_and_reopen(cua_server):
    origin = cua_server["origin"]
    books = cua_server["books"]
    shot_dir = books.parent / "cua_shots" / "11111111-1111-1111-1111-111111111111"
    browser = PlaywrightBrowser(origin, shot_dir, headless=True, faults_enabled=False)
    browser.open()
    try:
        browser.act(ComputerAction(goal="Open the workbook", action="open_workbook", expected_result="The workbook is open"))
        browser.act(ComputerAction(goal="Open purchase orders", action="select_sheet", sheet="Purchase_Orders", expected_result="Purchase orders"))
        browser.act(ComputerAction(goal="Find PO-1001", action="search_sheet", search_value="PO-1001", expected_result="PO-1001 is visible"))
        seen = browser.observe()
        assert seen.sheet == "Purchase_Orders"
        assert any(row.cells.get("PO ID") == "PO-1001" for row in seen.rows)
        assert seen.screenshot
        assert list(shot_dir.glob("*.png"))
        browser.act(ComputerAction(goal="Open the ledger", action="select_sheet", sheet="Invoice_Ledger", expected_result="The ledger is showing"))
        browser.act(ComputerAction(goal="Add a row", action="add_row", sheet="Invoice_Ledger", expected_result="A blank row exists"))
        browser.act(
            ComputerAction(
                goal="Type an invoice id",
                action="edit_cell",
                sheet="Invoice_Ledger",
                row=1,
                column="Invoice ID",
                value="INV-UI",
                expected_result="The cell shows INV-UI",
            )
        )
        edited = browser.observe()
        assert any(row.cells.get("Invoice ID") == "INV-UI" for row in edited.rows)
        browser.act(ComputerAction(goal="Save", action="save_workbook", expected_result="Saved"))
        assert browser.observe().save_state == "saved"
    finally:
        browser.close()

    again = PlaywrightBrowser(origin, shot_dir / "reopen", headless=True, faults_enabled=False)
    again.open()
    try:
        again.act(ComputerAction(goal="Reopen", action="open_workbook", expected_result="The workbook is open"))
        again.act(ComputerAction(goal="Find the typed invoice", action="search_sheet", search_value="INV-UI", expected_result="INV-UI is visible"))
        reopened = again.observe()
        assert any(row.cells.get("Invoice ID") == "INV-UI" for row in reopened.rows)
    finally:
        again.close()
    assert ledger_rows(books, "INV-UI")[0]["Invoice ID"] == "INV-UI"
    reset_workbook(books)


def test_playwright_clean_save_failure_and_lost_response(cua_server):
    origin = cua_server["origin"]
    books = cua_server["books"]
    app = cua_server["app"]

    def run(spec, key):
        reset_workbook(books)
        return run_workbook_task(
            store=app.state.store,
            extractor=TemplateExtractor(),
            workbook_path=books,
            browser_factory=lambda task_id: PlaywrightBrowser(
                origin,
                books.parent / "cua_shots" / task_id,
                headless=True,
                faults_enabled=True,
            ),
            origin=origin,
            instruction=INSTRUCTION,
            filename=spec.filename,
            pdf_bytes=invoice_pdf(spec),
            idempotency_key=key,
            faults_enabled=True,
            reset=False,
        )

    clean = run(CLEAN, "pw-clean")
    assert clean["verified"] is True
    assert clean["status"] == "completed"
    assert len(ledger_rows(books, "INV-2001")) == 1
    assert ledger_rows(books, "INV-2001")[0]["Status"] == "NEEDS_APPROVAL"
    assert any(event["detail"].get("screenshot") for event in clean["audit"] if event["step"] == "cua_act")

    failed_then_saved = run(RETRY_CLEAN, "pw-2010")
    assert failed_then_saved["verified"] is True
    assert len(ledger_rows(books, "INV-2010")) == 1
    assert any(event["step"] == "cua_recover" for event in failed_then_saved["audit"])

    lost = run(LOST_RESPONSE, "pw-2011")
    assert lost["verified"] is True
    assert len(ledger_rows(books, "INV-2011")) == 1


def test_playwright_remaining_invoice_cases(cua_server):
    origin = cua_server["origin"]
    books = cua_server["books"]
    app = cua_server["app"]

    def run(spec, key, *, pdf=None, filename=None, choice=None):
        if choice is None:
            reset_workbook(books)
        return run_workbook_task(
            store=app.state.store,
            extractor=TemplateExtractor(),
            workbook_path=books,
            browser_factory=lambda task_id: PlaywrightBrowser(
                origin,
                books.parent / "cua_shots" / task_id,
                headless=True,
                faults_enabled=False,
            ),
            origin=origin,
            instruction=INSTRUCTION,
            filename=filename or spec.filename,
            pdf_bytes=pdf if pdf is not None else invoice_pdf(spec),
            idempotency_key=key,
            faults_enabled=False,
            reset=False,
            vendor_choice=choice,
        )

    quantity = run(MISMATCH, "pw-qty")
    assert quantity["verified"] is True
    assert ledger_rows(books, "INV-2002")[0]["Status"] == "NEEDS_REVIEW"
    assert ledger_rows(books, "INV-2002")[0]["Exception"] == "QUANTITY_MISMATCH"

    duplicate = run(DUPLICATE, "pw-dup")
    assert duplicate["verified"] is True
    assert len(ledger_rows(books, "INV-3003")) == 1

    tax = run(TAX_ERROR, "pw-tax")
    assert tax["verified"] is True
    assert "TAX_MISMATCH" in ledger_rows(books, "INV-2100")[0]["Exception"]

    price = run(BAD_PRICE, "pw-price")
    assert price["verified"] is True
    assert ledger_rows(books, "INV-2012")[0]["Exception"] == "PRICE_MISMATCH"

    missing = run(MISSING_PO, "pw-missing")
    assert missing["verified"] is True
    assert "PO_NOT_FOUND" in ledger_rows(books, "INV-2200")[0]["Exception"]

    unknown = run(UNKNOWN_VENDOR, "pw-unknown")
    assert unknown["verified"] is True
    assert ledger_rows(books, "INV-7001") == []

    paused = run(AMBIGUOUS, "pw-helios")
    assert paused["status"] == "waiting_for_user"
    assert ledger_rows(books, "INV-5005") == []
    resumed = run(AMBIGUOUS, "pw-helios", choice="Helios Metals")
    assert resumed["verified"] is True
    assert ledger_rows(books, "INV-5005")[0]["Vendor"] == "Helios Metals"
    assert ledger_rows(books, "INV-5005")[0]["Status"] == "NEEDS_APPROVAL"

    scan = run(None, "pw-scan", pdf=poor_scan_pdf(), filename="scan-unreadable.pdf")
    assert scan["verified"] is True
    assert scan["decision"] == "missing_information"

