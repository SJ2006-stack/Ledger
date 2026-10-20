from pathlib import Path

import pytest

from app.config import Settings
from app.cua.controller import choose as deterministic_choose
from app.cua.recovery import ActionFault
from app.cua.schemas import COLUMNS, ComputerAction, PolicyError, validate_action
from app.cua.session import load_session, save_session
from app.cua.sql_ledger import SqlLedgerBrowser
from app.cua.worker import run_workbook_task
from app.documents import CLEAN, MISMATCH, invoice_pdf
from app.extractor import TemplateExtractor

REAL_LABELS = ("Orders FY26", "PO Register")
MISLEADING = "Draft Orders"


def _browser(store) -> SqlLedgerBrowser:
    return SqlLedgerBrowser(store.conn)


def _run(store, tmp_path, browser, key: str, *, spec=CLEAN, settings=None):
    return run_workbook_task(
        store=store,
        extractor=TemplateExtractor(),
        workbook_path=tmp_path / "session-only.xlsx",
        browser_factory=lambda task_id: browser,
        origin="http://127.0.0.1:8765",
        instruction="Record this invoice on the ledger.",
        filename=spec.filename,
        pdf_bytes=invoice_pdf(spec),
        idempotency_key=key,
        reset=False,
        settings=settings,
    )


def _groq_settings(tmp_path) -> Settings:
    return Settings(
        groq_api_key="present-but-unused",
        model="qwen/qwen3.8-27b",
        extractor="groq",
        db_path=tmp_path / "unused.db",
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


def test_model_action_is_the_action_executed(store, tmp_path, monkeypatch):
    browser = _recording(_browser(store))
    chooser = _ScriptedChooser(
        ComputerAction(
            goal="Open goods receipts before the usual vendor sheet",
            action="select_sheet",
            sheet="Goods_Receipts",
            expected_result="The Goods_Receipts sheet is showing",
        )
    )
    _install(monkeypatch, chooser)
    result = _run(store, tmp_path, browser, "sql-model-exec", settings=_groq_settings(tmp_path))
    assert result["status"] == "completed"
    assert result["verified"] is True
    assert browser.executed[0].action == "select_sheet"
    assert browser.executed[0].sheet == "Goods_Receipts"
    assert browser.executed[0].sheet == chooser.proposed[0].sheet
    assert chooser.choose_calls
    assert all(call["give_up"] for call in chooser.choose_calls)
    model_acts = [
        item for item in result["audit"]
        if item["step"] == "cua_act" and item["detail"].get("control") == "model" and item["outcome"] != "rejected"
    ]
    assert model_acts
    assert all(item["detail"].get("validation") == "accepted" for item in model_acts)
    recovery = [
        item for item in result["audit"]
        if item["step"] == "cua_act" and item["detail"].get("control") == "deterministic_recovery" and item["outcome"] == "ok"
    ]
    assert recovery
    assert any(item["step"] == "control_handoff" and item["outcome"] == "deterministic_recovery" for item in result["audit"])
    _assert_clean_row(browser)


def test_rejected_total_hands_off_and_recovery_writes_the_rules(store, tmp_path, monkeypatch):
    browser = _recording(_browser(store))
    chooser = _BadTotalChooser()
    _install(monkeypatch, chooser)
    result = _run(store, tmp_path, browser, "sql-model-reject", settings=_groq_settings(tmp_path))
    assert chooser.give_up
    assert len(chooser.proposals) >= 2
    assert chooser.choose_calls
    assert all(call["give_up"] for call in chooser.choose_calls)
    assert all(isinstance(call["observation"].url, str) for call in chooser.choose_calls)
    assert all(action.value != "1.00" for action in browser.executed)
    rejected = [item for item in result["audit"] if item["outcome"] == "rejected"]
    assert len(rejected) >= 2
    assert all(item["detail"].get("validation") == "rejected" for item in rejected)
    assert all(item["detail"].get("ok") is False for item in rejected)
    assert any("Total" in (item["detail"].get("gate_reason") or "") for item in rejected)
    assert any(item["step"] == "control_handoff" for item in result["audit"])
    assert result["status"] == "completed"
    assert result["verified"] is True
    _assert_clean_row(browser)


def test_a_second_blank_row_from_the_model_hands_off(store, tmp_path, monkeypatch):
    browser = _recording(_browser(store))
    chooser = _BlankRowChooser()
    _install(monkeypatch, chooser)
    result = _run(store, tmp_path, browser, "sql-blank-handoff", settings=_groq_settings(tmp_path))
    assert chooser.give_up
    assert browser.actions.count("add_row") == 1
    assert any(item["step"] == "control_handoff" for item in result["audit"])
    assert result["status"] == "completed"
    assert result["verified"] is True
    _assert_clean_row(browser)


def test_renamed_po_tab_is_used_and_a_partial_tab_is_ignored(store, tmp_path):
    browser = _recording(_RenamedPo(store.conn))
    result = _run(store, tmp_path, browser, "sql-renamed-tabs")
    assert result["status"] == "completed"
    assert result["verified"] is True
    selected = [action.sheet for action in browser.executed if action.action == "select_sheet"]
    assert REAL_LABELS[0] in selected
    assert MISLEADING not in selected
    adapted = next(item for item in result["audit"] if item["step"] == "sheet_adapted")
    assert adapted["detail"]["label"] == REAL_LABELS[0]
    _assert_clean_row(browser)


def test_scripted_chooser_selects_the_renamed_tab(store, tmp_path, monkeypatch):
    browser = _recording(_RenamedPo(store.conn))
    chooser = _RenamedTabChooser()
    _install(monkeypatch, chooser)
    result = _run(store, tmp_path, browser, "sql-model-renamed", settings=_groq_settings(tmp_path))
    assert chooser.proposed
    assert browser.executed[0].action == "select_sheet"
    assert browser.executed[0].sheet == chooser.proposed[0].sheet
    assert browser.executed[0].sheet == REAL_LABELS[0]
    assert MISLEADING not in {action.sheet for action in browser.executed}
    assert all(call["give_up"] for call in chooser.choose_calls)
    model = next(
        item for item in result["audit"]
        if item["step"] == "cua_act" and item["detail"].get("sheet") == REAL_LABELS[0] and item["detail"].get("control") == "model"
    )
    assert model["detail"]["action"] == "select_sheet"
    assert model["detail"]["validation"] == "accepted"
    assert result["verified"] is True
    _assert_clean_row(browser)


def test_lost_confirmation_stops_before_complete_then_resume_verifies(store, tmp_path):
    browser = _LostBeforeComplete(store.conn)
    first = _run(store, tmp_path, browser, "sql-lost-before-complete")
    assert first["status"] == "failed"
    assert "confirmation" in first["explanation"].casefold()
    assert browser.saves == 1
    assert _ids(browser, "INV-2001") == 1
    saved = _clean_row(browser)
    assert saved["Status"] == "NEEDS_APPROVAL"
    second = _run(store, tmp_path, browser, "sql-lost-before-complete")
    assert second["status"] == "completed"
    assert second["verified"] is True
    assert browser.saves == 1
    assert _ids(browser, "INV-2001") == 1
    assert "save_workbook" not in browser.actions
    assert "add_row" not in browser.actions
    assert "edit_cell" not in browser.actions
    assert any(
        item["detail"].get("action") == "search_sheet" and item["detail"].get("search") == "INV-2001"
        for item in second["audit"]
        if item["step"] == "cua_act"
    )
    verified = next(item for item in second["audit"] if item["step"] == "cua_verify")
    assert verified["outcome"] == "verified"


def test_harbor_quantity_mismatch_is_held_on_the_sql_ledger(store, tmp_path):
    browser = _browser(store)
    result = _run(store, tmp_path, browser, "sql-harbor", spec=MISMATCH)
    assert result["status"] == "needs_review"
    assert result["decision"] == "needs_review"
    assert result["verified"] is True
    rows = [row for row in browser.read_back() if row["Invoice ID"] == "INV-2002"]
    assert len(rows) == 1
    assert rows[0]["Status"] == "NEEDS_REVIEW"
    assert rows[0]["Exception"] == "QUANTITY_MISMATCH"
    assert "approved" not in result["explanation"].casefold()


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


class _Recording:
    def __init__(self, browser: SqlLedgerBrowser) -> None:
        self._browser = browser
        self.executed: list[ComputerAction] = []

    def __getattr__(self, name: str):
        return getattr(self._browser, name)

    def act(self, action: ComputerAction) -> None:
        self.executed.append(action)
        self._browser.act(action)


class _RenamedPo(SqlLedgerBrowser):
    def open(self, fault: str | None = None, *, reset: bool = False) -> None:
        super().open(fault, reset=reset)
        rows = self.sheets.pop("Purchase_Orders")
        headers = self.headers.pop("Purchase_Orders")
        label = REAL_LABELS[0]
        self.sheets[label] = rows
        self.headers[label] = headers
        self.sheets[MISLEADING] = [["PO-1001", "draft"]]
        self.headers[MISLEADING] = ["PO ID", "Note"]
        self.order = [MISLEADING, "Vendors", label, "Goods_Receipts", "Invoice_Ledger"]


class _LostBeforeComplete(SqlLedgerBrowser):
    def __init__(self, conn) -> None:
        super().__init__(conn)
        self._interrupt = True

    def _save(self) -> None:
        super()._save()
        if self._interrupt:
            self._interrupt = False
            self.dialog = "lost_response"
            self.save_state = "unconfirmed"
            self._stop = True

    def observe(self, *, capture: bool = True):
        if getattr(self, "_stop", False):
            self._stop = False
            raise ActionFault(
                "save_confirmation_missing",
                "The save confirmation was lost before the task was recorded.",
            )
        return super().observe(capture=capture)


class _ChooserBase:
    def __init__(self) -> None:
        self.failures = 0
        self.last_error = ""
        self.proposed: list[ComputerAction] = []
        self.choose_calls: list[dict] = []

    @property
    def give_up(self) -> bool:
        return self.failures >= 2

    def note_failure(self, reason: str) -> None:
        self.last_error = reason
        self.failures += 1


class _ScriptedChooser(_ChooserBase):
    def __init__(self, action: ComputerAction) -> None:
        super().__init__()
        self.action = action

    def propose(self, job, obs, mismatch: str):
        del job, obs, mismatch
        if self.proposed:
            self.failures = 2
            return None
        self.proposed.append(self.action)
        return self.action


class _BadTotalChooser(_ChooserBase):
    def propose(self, job, obs, mismatch: str):
        del job, obs, mismatch
        action = ComputerAction(
            goal="Change the total",
            action="edit_cell",
            sheet="Invoice_Ledger",
            row=1,
            column="Total",
            value="1.00",
            expected_result="Total becomes 1.00",
        )
        self.proposed.append(action)
        self.proposals = self.proposed
        return action


class _BlankRowChooser(_ChooserBase):
    def propose(self, job, obs, mismatch: str):
        del job, obs, mismatch
        action = ComputerAction(
            goal="Add a blank ledger row",
            action="add_row",
            sheet="Invoice_Ledger",
            expected_result="A blank ledger row is visible",
        )
        self.proposed.append(action)
        return action


class _RenamedTabChooser(_ChooserBase):
    def propose(self, job, obs, mismatch: str):
        del job, mismatch
        if self.proposed:
            self.failures = 2
            return None
        label = ""
        for tab in obs.tabs:
            if {"PO ID", "Ordered Qty", "Unit Price"} <= set(tab.headers):
                label = tab.label
                break
        if not label:
            self.failures = 2
            return None
        action = ComputerAction(
            goal=f"Open {label}",
            action="select_sheet",
            sheet=label,
            expected_result=f"The {label} sheet is showing",
        )
        self.proposed.append(action)
        return action


def _install(monkeypatch, chooser: _ChooserBase) -> None:
    def explode(*_args, **_kwargs):
        raise AssertionError("offline test called Groq")

    monkeypatch.setattr("app.cua.chooser.generate_json", explode)
    monkeypatch.setattr("app.cua.worker.ActionChooser", lambda _settings: chooser)

    def spy(job, progress, obs):
        chooser.choose_calls.append({"give_up": chooser.give_up, "observation": obs})
        return deterministic_choose(job, progress, obs)

    monkeypatch.setattr("app.cua.controller.choose", spy)


def _recording(browser: SqlLedgerBrowser) -> _Recording:
    return _Recording(browser)


def _clean_row(browser) -> dict[str, str]:
    rows = [row for row in browser.read_back() if row["Invoice ID"] == "INV-2001"]
    assert len(rows) == 1
    return rows[0]


def _assert_clean_row(browser) -> None:
    row = _clean_row(browser)
    assert row["Vendor"] == "Northwind Supplies"
    assert row["Tax ID"] == "27AABCU9603R1ZM"
    assert row["Invoice Date"] == "2026-10-01"
    assert row["PO ID"] == "PO-1001"
    assert row["GRN ID"] == "GRN-1001"
    assert row["Subtotal"] == "5000.00"
    assert row["Tax Rate"] == "18"
    assert row["Tax Amount"] == "900.00"
    assert row["Total"] == "5900.00"
    assert row["Status"] == "NEEDS_APPROVAL"
    assert row["Exception"] == ""
    assert row["Source PDF"] == CLEAN.filename
    assert _ids(browser, "INV-2001") == 1
