from pathlib import Path

import pytest

from app.config import Settings
from app.cua.chooser import ActionChooser, gate_model_action
from app.cua.controller import Job, run_controller
from app.cua.memory_browser import MemoryBrowser
from app.cua.schemas import ComputerAction, Observation, PolicyError
from app.llm import JsonReply

ORIGIN = "http://127.0.0.1:8765"


def _settings() -> Settings:
    return Settings(
        groq_api_key="test-key",
        model="qwen/qwen3.8-27b",
        extractor="groq",
        db_path=Path("/tmp/unused.db"),
    )


def _job() -> Job:
    return Job(
        invoice_id="INV-2001",
        source_pdf="northwind-inv-2001.pdf",
        vendor_query="Northwind",
        vendor_expect="skip",
        po_query="",
        po_expect="none",
        grn_query="",
        grn_expect="none",
        intended={
            "Invoice ID": "INV-2001",
            "Status": "NEEDS_APPROVAL",
            "Total": "5900.00",
        },
    )


def test_gate_blocks_disallowed_writes():
    intended = {"Status": "NEEDS_APPROVAL", "Total": "5900.00"}
    with pytest.raises(PolicyError):
        gate_model_action(
            ComputerAction(
                goal="Post it",
                action="edit_cell",
                sheet="Invoice_Ledger",
                row=1,
                column="Status",
                value="POSTED",
                expected_result="posted",
            ),
            intended,
        )
    with pytest.raises(PolicyError):
        gate_model_action(
            ComputerAction(
                goal="Add a row",
                action="add_row",
                sheet="Invoice_Ledger",
                expected_result="row",
            ),
            None,
        )


def test_chooser_returns_valid_action_from_groq(monkeypatch):
    def fake_generate(api_key, model, prompt, *, max_tokens=4096):
        del api_key, model, prompt, max_tokens
        return JsonReply(
            payload={
                "goal": "Open the ledger",
                "action": "select_sheet",
                "sheet": "Invoice_Ledger",
                "expected_result": "Ledger is visible",
            },
            text="{}",
            input_tokens=1,
            output_tokens=2,
            source="groq_json",
        )

    monkeypatch.setattr("app.cua.chooser.generate_json", fake_generate)
    chooser = ActionChooser(_settings())
    action = chooser.propose(_job(), Observation(url="http://x/workbook"), "")
    assert action is not None
    assert action.action == "select_sheet"
    assert action.sheet == "Invoice_Ledger"
    assert not chooser.give_up


def test_only_the_first_proposed_action_is_taken(monkeypatch):
    def fake_generate(api_key, model, prompt, *, max_tokens=4096):
        del api_key, model, prompt, max_tokens
        return JsonReply(
            payload={
                "actions": [
                    {"goal": "Open vendors", "action": "select_sheet", "sheet": "Vendors", "expected_result": "Vendors"},
                    {"goal": "Skip ahead", "action": "save_workbook", "expected_result": "Saved"},
                ]
            },
            text="{}",
            input_tokens=1,
            output_tokens=2,
            source="groq_json",
        )

    monkeypatch.setattr("app.cua.chooser.generate_json", fake_generate)
    action = ActionChooser(_settings()).propose(_job(), Observation(url="http://x/workbook"), "")
    assert action is not None
    assert action.action == "select_sheet"
    assert action.sheet == "Vendors"


def test_chooser_gives_up_after_two_bad_replies(monkeypatch):
    def boom(*args, **kwargs):
        del args, kwargs
        raise ValueError("bad json")

    monkeypatch.setattr("app.cua.chooser.generate_json", boom)
    chooser = ActionChooser(_settings())
    assert chooser.propose(_job(), Observation(url=""), "") is None
    assert chooser.failures == 1
    assert chooser.propose(_job(), Observation(url=""), "") is None
    assert chooser.give_up
    assert chooser.propose(_job(), Observation(url=""), "") is None


def test_illegal_model_actions_fall_back_to_deterministic_path(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_generate(api_key, model, prompt, *, max_tokens=4096):
        del api_key, model, prompt, max_tokens
        calls["n"] += 1
        return JsonReply(
            payload={
                "goal": "Mark it posted",
                "action": "edit_cell",
                "sheet": "Invoice_Ledger",
                "row": 1,
                "column": "Status",
                "value": "POSTED",
                "expected_result": "Status becomes POSTED",
            },
            text="{}",
            input_tokens=1,
            output_tokens=2,
            source="groq_json",
        )

    monkeypatch.setattr("app.cua.chooser.generate_json", fake_generate)
    browser = MemoryBrowser(tmp_path / "chooser-book.xlsx")
    browser.open(reset=True)
    chooser = ActionChooser(_settings())
    progress = run_controller(browser, _job(), origin=ORIGIN, chooser=chooser, budget=8)
    assert chooser.give_up
    assert calls["n"] >= 2
    assert progress.outcome == "READY_TO_VERIFY"
    assert all(record.action != "edit_cell" for record in progress.records)
