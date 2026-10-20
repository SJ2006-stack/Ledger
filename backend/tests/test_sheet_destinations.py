from pathlib import Path

import pytest

from app.cua.recovery import ActionFault
from app.cua.schemas import COLUMNS, ComputerAction, PolicyError, validate_action
from app.cua.session import load_session, save_session
from app.cua.sql_ledger import SqlLedgerBrowser
from app.cua.worker import run_workbook_task
from app.documents import CLEAN, invoice_pdf
from app.extractor import TemplateExtractor

REAL_LABELS = ("Orders FY26", "PO Register")
MISLEADING = "Draft Orders"


def _browser(store) -> SqlLedgerBrowser:
    return SqlLedgerBrowser(store.conn)


def _run(store, tmp_path, browser, key: str):
    return run_workbook_task(
        store=store,
        extractor=TemplateExtractor(),
        workbook_path=tmp_path / "session-only.xlsx",
        browser_factory=lambda task_id: browser,
        origin="http://127.0.0.1:8765",
        instruction="Record this invoice on the ledger.",
        filename=CLEAN.filename,
        pdf_bytes=invoice_pdf(CLEAN),
        idempotency_key=key,
        reset=False,
    )


def test_sql_ledger_records_a_clean_invoice(store, tmp_path):
    browser = _browser(store)
    result = _run(store, tmp_path, browser, "sql-clean-1")
    assert result["status"] == "completed"
    assert result["verified"] is True
    assert _ids(browser, "INV-2001") == 1
    assert _ids(browser, "INV-3003") == 1
    assert "/ledger" in browser.url
    assert not (tmp_path / "session-only.xlsx").exists()
    root = Path(__file__).resolve().parents[1] / "app" / "cua"
    for name in ("controller.py", "chooser.py", "sql_ledger.py"):
        text = (root / name).read_text(encoding="utf-8")
        for label in (*REAL_LABELS, MISLEADING):
            assert label not in text


def test_a_second_blank_row_is_rejected(store):
    browser = _browser(store)
    browser.open()
    browser.act(
        ComputerAction(
            goal="Add a blank ledger row",
            action="add_row",
            sheet="Invoice_Ledger",
            expected_result="A blank ledger row is visible",
        )
    )
    with pytest.raises(ActionFault) as caught:
        browser.act(
            ComputerAction(
                goal="Add another blank ledger row",
                action="add_row",
                sheet="Invoice_Ledger",
                expected_result="A blank ledger row is visible",
            )
        )
    assert caught.value.code == "write_rejected"


def test_resume_after_save_does_not_write_the_row_again(store, tmp_path):
    browser = _browser(store)
    first = _run(store, tmp_path, browser, "sql-resume-save")
    assert first["verified"] is True
    task = store.task_by_id(first["id"])
    task["status"] = "running"
    store.update_task(task)
    assert load_session(tmp_path / "session-only.xlsx", first["id"]).get("checkpoint")
    saves = browser.saves
    second = _run(store, tmp_path, browser, "sql-resume-save")
    assert second["verified"] is True
    assert browser.saves == saves
    assert _ids(browser, "INV-2001") == 1


def test_resume_blocks_a_partial_row_and_a_conflicting_row(store, tmp_path):
    browser = _browser(store)
    first = _run(store, tmp_path, browser, "sql-resume-partial")
    _rewrite(browser, "INV-2001", keep={"Invoice ID", "Vendor"})
    task = store.task_by_id(first["id"])
    task["status"] = "running"
    store.update_task(task)
    saves = browser.saves
    partial = _run(store, tmp_path, browser, "sql-resume-partial")
    assert partial["status"] == "needs_recovery"
    assert "partial" in partial["explanation"].casefold()
    assert browser.saves == saves

    _rewrite(browser, "INV-2001", keep=set(COLUMNS["Invoice_Ledger"]), Total="1.00")
    task = store.task_by_id(first["id"])
    task["status"] = "needs_recovery"
    store.update_task(task)
    conflict = _run(store, tmp_path, browser, "sql-resume-partial")
    assert conflict["status"] == "needs_recovery"
    assert "conflict" in conflict["explanation"].casefold()
    assert browser.saves == saves
    assert _ids(browser, "INV-2001") == 1


def test_resume_after_lost_confirmation_verifies_the_existing_row(store, tmp_path):
    browser = _browser(store)
    first = _run(store, tmp_path, browser, "sql-resume-lost")
    task = store.task_by_id(first["id"])
    task["status"] = "running"
    store.update_task(task)
    session = load_session(tmp_path / "session-only.xlsx", first["id"])
    session["checkpoint"]["last_confirmed_action"] = "save_workbook"
    save_session(tmp_path / "session-only.xlsx", first["id"], session)
    saves = browser.saves
    second = _run(store, tmp_path, browser, "sql-resume-lost")
    assert second["verified"] is True
    assert browser.saves == saves
    assert _ids(browser, "INV-2001") == 1


def test_approval_is_idempotent_and_human_only(store):
    book = _browser(store).book
    book.replace_ledger(
        [
            *_row("INV-3003", "NEEDS_APPROVAL"),
            *_row("INV-9", "NEEDS_APPROVAL"),
        ]
    )
    first = book.approve("INV-9")
    second = book.approve("INV-9")
    assert first["changed"] is True
    assert first["status"] == "APPROVED"
    assert second["changed"] is False
    assert second["status"] == "APPROVED"
    with pytest.raises(PolicyError):
        validate_action(
            ComputerAction(
                goal="Approve it",
                action="edit_cell",
                sheet="Invoice_Ledger",
                row=1,
                column="Status",
                value="APPROVED",
                expected_result="Approved",
            ),
            origin="http://127.0.0.1:8765",
        )


def _ids(browser: SqlLedgerBrowser, invoice_id: str) -> int:
    return sum(1 for row in browser.book.ledger_dicts() if row["Invoice ID"] == invoice_id)


def _rewrite(browser: SqlLedgerBrowser, invoice_id: str, *, keep: set[str], **overrides: str) -> None:
    headers = list(COLUMNS["Invoice_Ledger"])
    rows = browser.book.grids()["Invoice_Ledger"]
    for row in rows:
        if row[headers.index("Invoice ID")] != invoice_id:
            continue
        for index, column in enumerate(headers):
            if column in overrides:
                row[index] = overrides[column]
            elif column not in keep:
                row[index] = ""
    browser.book.replace_ledger(rows)


def _row(invoice_id: str, status: str) -> list[list[str]]:
    headers = list(COLUMNS["Invoice_Ledger"])
    row = [""] * len(headers)
    row[headers.index("Invoice ID")] = invoice_id
    row[headers.index("Status")] = status
    row[headers.index("Vendor")] = "Apex Industrial"
    row[headers.index("Total")] = "2360.00"
    return [row]
