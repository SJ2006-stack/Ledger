from pathlib import Path

from app.config import Settings
from app.cua.memory_browser import MemoryBrowser
from app.cua.session import load_session
from app.cua.worker import run_workbook_task
from app.documents import AMBIGUOUS, CLEAN, invoice_pdf
from app.extractor import ExtractionError, TemplateExtractor

ORIGIN = "http://127.0.0.1:8765"
INSTRUCTION = "Process the latest invoice against the finance workbook."


class _BoomExtractor:
    def extract(self, document_text: str, filename: str) -> object:
        del document_text, filename
        raise ExtractionError("GROQ_API_KEY is not set.")


def _run(store, path, spec, key, *, extractor=None, reset=True, choice=None, settings=None):
    browser = MemoryBrowser(path)
    return run_workbook_task(
        store=store,
        extractor=extractor or TemplateExtractor(),
        workbook_path=path,
        browser_factory=lambda task_id: browser,
        origin=ORIGIN,
        instruction=INSTRUCTION,
        filename=spec.filename,
        pdf_bytes=invoice_pdf(spec),
        idempotency_key=key,
        reset=reset,
        vendor_choice=choice,
        settings=settings,
    )


def test_completed_task_is_idempotent(store, tmp_path):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    first = _run(store, path, CLEAN, "cua-idem-1")
    assert first["status"] == "completed"
    audit_count = len(first["audit"])
    second = _run(store, path, CLEAN, "cua-idem-1", reset=False)
    assert second["status"] == "completed"
    assert second["id"] == first["id"]
    assert len(second["audit"]) == audit_count


def test_extract_failure_marks_task_failed(store, tmp_path):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    result = _run(store, path, CLEAN, "cua-extract-fail", extractor=_BoomExtractor())
    assert result["status"] == "failed"
    assert "GROQ_API_KEY" in result["explanation"]
    outcomes = {(item["step"], item["outcome"]) for item in result["audit"]}
    assert ("extract_fields", "failed") in outcomes


def test_waiting_for_user_without_choice_returns_cached_pause(store, tmp_path):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    paused = _run(store, path, AMBIGUOUS, "cua-pause-1")
    assert paused["status"] == "waiting_for_user"
    again = _run(store, path, AMBIGUOUS, "cua-pause-1", reset=False)
    assert again["status"] == "waiting_for_user"
    assert again["id"] == paused["id"]


def test_vendor_choice_resumes_paused_task(store, tmp_path):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    paused = _run(store, path, AMBIGUOUS, "cua-resume-1")
    assert paused["status"] == "waiting_for_user"
    session = load_session(path, paused["id"])
    assert session is not None
    assert "Helios Metals" in session["candidates"]
    resumed = _run(
        store,
        path,
        AMBIGUOUS,
        "cua-resume-1",
        reset=False,
        choice="Helios Metals",
    )
    assert resumed["status"] == "completed"
    assert resumed["verified"] is True
    assert resumed["id"] == paused["id"]


def test_bad_vendor_choice_stays_waiting(store, tmp_path):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    paused = _run(store, path, AMBIGUOUS, "cua-bad-choice")
    assert paused["status"] == "waiting_for_user"
    stuck = _run(
        store,
        path,
        AMBIGUOUS,
        "cua-bad-choice",
        reset=False,
        choice="Not A Real Vendor",
    )
    assert stuck["status"] == "waiting_for_user"
    assert "not one of the vendor candidates" in stuck["explanation"]


def test_verify_failure_marks_task_failed(store, tmp_path, monkeypatch):
    path = tmp_path / "Finance_AP_Ledger.xlsx"

    def fake_verify(path_arg, *, invoice_id, expect_row, intended=None, source_pdf=""):
        del path_arg, invoice_id, expect_row, intended, source_pdf
        return {
            "ok": False,
            "mismatches": ["Total: workbook has '1.00', intended '5900.00'"],
            "rows": [],
            "checked_at": "now",
        }

    monkeypatch.setattr("app.cua.worker.verify_ledger", fake_verify)
    result = _run(store, path, CLEAN, "cua-verify-fail")
    assert result["status"] == "failed"
    assert result["verified"] is False
    assert "verification did not pass" in result["explanation"].casefold()


def test_template_settings_skip_model_chooser(store, tmp_path):
    path = tmp_path / "Finance_AP_Ledger.xlsx"
    settings = Settings(
        groq_api_key="present-but-ignored",
        model="qwen/qwen3.8-27b",
        extractor="template",
        db_path=tmp_path / "unused.db",
    )
    result = _run(store, path, CLEAN, "cua-no-model", settings=settings)
    assert result["status"] == "completed"
    assert result["verified"] is True
