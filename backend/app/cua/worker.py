"""Run an invoice through the existing rules, then carry the result into the workbook UI."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.accounting import AccountingStore
from app.config import Settings
from app.cua.chooser import ActionChooser
from app.cua.controller import Job, _plan_snapshot, run_controller
from app.cua.lessons import load_learned, remember_lesson, system_prompt
from app.cua.recovery import classify_fault
from app.cua.session import extraction_from_payload, extraction_payload, load_session, save_session
from app.cua.workbook_io import verify_ledger, verify_rows
from app.documents import store_task_pdf
from app.extractor import Extraction, ExtractionError, Extractor, validate_extraction
from app.matcher import MatchReport, action_for, decide, explain, match_invoice
from app.money import bps_to_str, cents_to_str
from app.orchestrator import _settle, _should_persist
from app.parser import parse_pdf
from app.schemas import Decision, Issue

FAULT_FILES = {
    "northwind-inv-2010.pdf": "save_failure",
    "northwind-inv-2011.pdf": "lost_response",
}

ISSUE_CODES = {
    Issue.QUANTITY_MISMATCH.value: "QUANTITY_MISMATCH",
    Issue.PRICE_MISMATCH.value: "PRICE_MISMATCH",
    Issue.TAX_ARITHMETIC.value: "TAX_MISMATCH",
    Issue.DUPLICATE_INVOICE.value: "DUPLICATE_INVOICE",
    Issue.PO_MISSING.value: "PO_NOT_FOUND",
    Issue.GRN_MISSING.value: "GRN_NOT_FOUND",
    Issue.VENDOR_AMBIGUOUS.value: "VENDOR_AMBIGUOUS",
    Issue.VENDOR_UNKNOWN.value: "VENDOR_NOT_FOUND",
    Issue.POOR_SCAN.value: "EXTRACTION_FAILED",
    Issue.MISSING_REQUIRED_FIELDS.value: "EXTRACTION_FAILED",
}


def run_workbook_task(
    *,
    store: AccountingStore,
    extractor: Extractor,
    workbook_path: Path,
    browser_factory,
    origin: str,
    instruction: str,
    filename: str,
    pdf_bytes: bytes,
    idempotency_key: str,
    faults_enabled: bool | None = None,
    on_event: Callable[[dict], None] | None = None,
    reset: bool = False,
    vendor_choice: str | None = None,
    settings: Settings | None = None,
) -> dict:
    if faults_enabled is None:
        faults_enabled = os.environ.get("CUA_FAULTS") == "1"
    existing = store.task_by_key(idempotency_key)
    if existing is not None and existing["status"] == "running":
        session = load_session(workbook_path, existing["id"])
        if not session or not session.get("checkpoint"):
            existing["status"] = "failed"
            existing["explanation"] = "The task was interrupted before a confirmed action."
            existing["updated_at"] = _now()
            store.update_task(existing)
            return _view(store, existing)
    if existing is not None and existing["status"] not in {"failed", "waiting_for_user", "running", "needs_recovery"} and not vendor_choice:
        return _view(store, existing)
    if existing is not None and existing["status"] == "waiting_for_user" and not vendor_choice:
        return _view(store, existing)

    task = _ensure_task(store, existing, instruction, filename, idempotency_key)
    store_task_pdf(task["id"], pdf_bytes)
    choice = vendor_choice
    session = load_session(workbook_path, task["id"]) if choice else None
    if choice:
        if session is None:
            task["status"] = "failed"
            task["explanation"] = "There is no paused workbook task to resume."
            task["updated_at"] = _now()
            store.update_task(task)
            return _view(store, task)
        allowed = session.get("candidates") or []
        if choice not in allowed:
            task["status"] = "waiting_for_user"
            task["explanation"] = f"{choice} is not one of the vendor candidates."
            task["updated_at"] = _now()
            store.update_task(task)
            return _view(store, task)
        extraction = extraction_from_payload(session["extraction"])
        _audit(store, task["id"], "clarification", "received", {"vendor_name": choice}, on_event)
    else:
        prepared = _prepare(store, extractor, task, pdf_bytes, on_event)
        if prepared is None:
            return _view(store, store.task_by_id(task["id"]) or task)
        extraction, early = prepared
        if early is not None:
            report, decision = early
            return _drive(
                store, workbook_path, browser_factory, origin, task, filename, extraction, report, decision,
                faults_enabled, on_event, reset, None, settings,
            )
    report = match_invoice(store, extraction, task["idempotency_key"], choice)
    decision = decide(report.issues)
    final_action = action_for(decision)
    _audit(
        store,
        task["id"],
        "match_records",
        report.vendor_status,
        {"issues": [issue.value for issue in report.issues], "vendor_id": report.vendor_id, "candidates": report.candidates},
        on_event,
    )
    _audit(
        store,
        task["id"],
        "decide",
        decision.value,
        {
            "final_action": final_action.value,
            "model_action": extraction.proposed_action,
            "overridden": bool(extraction.proposed_action) and extraction.proposed_action != final_action.value,
        },
        on_event,
    )
    return _drive(
        store, workbook_path, browser_factory, origin, task, filename, extraction, report, decision,
        faults_enabled, on_event, reset and not choice, choice, settings,
    )


def _prepare(store, extractor, task, pdf_bytes, on_event):
    _audit(store, task["id"], "received", "accepted", {"instruction": task["instruction"]}, on_event)
    parsed = parse_pdf(pdf_bytes)
    _audit(
        store,
        task["id"],
        "parse_document",
        parsed.quality,
        {"pages": parsed.pages, "used_ocr": parsed.used_ocr, "ocr_confidence": parsed.ocr_confidence, "characters": len(parsed.text)},
        on_event,
    )
    if parsed.quality == "poor":
        second = parse_pdf(pdf_bytes)
        _audit(
            store,
            task["id"],
            "extract_fields",
            "request_clarification",
            {"reason": "poor_scan", "second_quality": second.quality},
            on_event,
        )
        report = MatchReport(vendor_status="unknown", vendor_id=None, vendor_name=None)
        report.issues.append(Issue.POOR_SCAN)
        return None, (report, Decision.MISSING_INFORMATION)
    try:
        extraction = extractor.extract(parsed.text, task["filename"] or "invoice.pdf")
    except ExtractionError as exc:
        _audit(store, task["id"], "extract_fields", "failed", {"error": str(exc), "thinking": exc.thinking}, on_event)
        task["status"] = "failed"
        task["explanation"] = str(exc)
        task["updated_at"] = _now()
        store.update_task(task)
        return None
    _audit(
        store,
        task["id"],
        "extract_fields",
        "extracted",
        {
            "model": extraction.model,
            "invoice_number": extraction.invoice_number,
            "vendor_name": extraction.vendor_name,
            "proposed_action": extraction.proposed_action,
            "thinking": extraction.thinking,
            "input_tokens": extraction.input_tokens,
            "output_tokens": extraction.output_tokens,
        },
        on_event,
    )
    validation = validate_extraction(extraction)
    _audit(store, task["id"], "validate_fields", "ok" if validation.ok else "rejected", {"missing": list(validation.missing)}, on_event)
    if not validation.ok:
        report = MatchReport(vendor_status="unknown", vendor_id=None, vendor_name=extraction.vendor_name)
        report.issues.append(Issue.MISSING_REQUIRED_FIELDS)
        return extraction, (report, Decision.MISSING_INFORMATION)
    return extraction, None


def _drive(
    store,
    workbook_path,
    browser_factory,
    origin,
    task,
    filename,
    extraction: Extraction | None,
    report: MatchReport,
    decision: Decision,
    faults_enabled: bool,
    on_event,
    reset: bool,
    vendor_choice: str | None,
    settings: Settings | None,
) -> dict:
    job = _job(filename, extraction, report, decision, vendor_choice)
    fault = FAULT_FILES.get(filename) if faults_enabled else None
    save_session(
        workbook_path,
        task["id"],
        {
            "instruction": task["instruction"],
            "filename": filename,
            "idempotency_key": task["idempotency_key"],
            "candidates": report.candidates,
            "extraction": extraction_payload(extraction) if extraction is not None else None,
            "phase": "waiting" if job.needs_clarification else "running",
            "invoice_id": job.invoice_id,
        },
    )
    chooser = _live_chooser(settings)
    use_lessons = getattr(chooser, "use_lessons", None)
    if use_lessons is not None:
        use_lessons(system_prompt(load_learned(store.conn)))

    def _on_step(payload: dict) -> None:
        if payload.get("phase") == "handoff":
            _audit(
                store,
                task["id"],
                "control_handoff",
                "deterministic_recovery",
                {
                    "from": "model",
                    "to": "deterministic_recovery",
                    "reason": "Two invalid proposed actions",
                    "last_error": payload.get("recovery") or "",
                    "goal": payload.get("goal") or "",
                    "control": payload.get("control") or "deterministic_recovery",
                    "plan": payload.get("plan"),
                },
                on_event,
            )
            return
        if payload.get("phase") == "rejected":
            _audit(
                store,
                task["id"],
                "cua_act",
                "rejected",
                {
                    "goal": payload.get("goal") or "",
                    "action": payload.get("action") or "",
                    "sheet": payload.get("sheet") or "",
                    "observed": payload.get("observed") or "",
                    "validation": "rejected",
                    "gate_reason": payload.get("gate_reason") or "",
                    "control": payload.get("control") or "",
                    "ok": False,
                    "plan": payload.get("plan"),
                    "recovery": "",
                },
                on_event,
            )
            return
        about = payload["phase"] == "about_to"
        recovery = payload.get("recovery") or ""
        if about:
            outcome = "about_to"
        elif recovery and payload["ok"]:
            outcome = "recovered"
        elif recovery:
            outcome = "recovering"
        elif payload["ok"]:
            outcome = "ok"
        else:
            outcome = "retry"
        detail = {
            "goal": payload["goal"],
            "action": payload["action"],
            "sheet": payload["sheet"],
            "expected_result": payload["expected_result"],
            "observed": payload["observed"],
            "recovery": recovery,
            "screenshot": payload["screenshot"],
            "duration_ms": payload.get("duration_ms", 0),
            "attempt": payload.get("attempt", 0),
            "status": "RUNNING" if about or not recovery else "RECOVERING",
            "plan": payload.get("plan"),
            "row": payload.get("row"),
            "column": payload.get("column"),
            "value": payload.get("value"),
            "search": payload.get("search") or "",
            "control": payload.get("control") or "",
            "validation": payload.get("validation") or "",
            "gate_reason": payload.get("gate_reason") or "",
            "ok": payload.get("ok"),
        }
        _audit(
            store,
            task["id"],
            "cua_act" if about or not recovery else "cua_recover",
            outcome,
            detail,
            on_event,
        )
        adapted = payload.get("sheet_adapted")
        if adapted:
            _audit(
                store,
                task["id"],
                "sheet_adapted",
                "adapted",
                {
                    "label": adapted.get("label"),
                    "observation": adapted.get("observation"),
                    "evidence": adapted.get("evidence"),
                    "plan": payload.get("plan"),
                },
                on_event,
            )
        if payload["phase"] == "done" and payload["ok"]:
            _write_checkpoint(workbook_path, task, job, payload)

    browser = None
    progress = None
    try:
        browser = browser_factory(task["id"])
        browser.open(fault, reset=reset)
        progress = run_controller(browser, job, origin=origin, on_step=_on_step, chooser=chooser)
    except Exception as exc:
        code = classify_fault(exc)
        task["status"] = "failed"
        task["explanation"] = f"{code}: {exc}"
        task["verified"] = False
        task["updated_at"] = _now()
        store.update_task(task)
        return _view(store, task)
    finally:
        if browser is not None:
            browser.close()
    if progress is None:
        if task["status"] == "running":
            task["status"] = "failed"
            task["explanation"] = task["explanation"] or "The task stopped without a result."
            task["verified"] = False
            task["updated_at"] = _now()
            store.update_task(task)
        return _view(store, task)
    if progress.outcome == "WAITING_FOR_USER":
        _audit(
            store,
            task["id"],
            "clarification",
            "waiting",
            {"candidates": report.candidates, "reason": progress.reason},
            on_event,
        )
        task["status"] = "waiting_for_user"
        task["decision"] = decision.value
        task["explanation"] = progress.reason
        task["verified"] = False
        task["issues"] = [issue.value for issue in report.issues]
        task["model_action"] = extraction.proposed_action if extraction else None
        task["final_action"] = action_for(decision).value
        task["updated_at"] = _now()
        store.update_task(task)
        return _view(store, task)

    if progress.outcome == "NEEDS_RECOVERY":
        task["status"] = "needs_recovery"
        task["decision"] = decision.value
        task["explanation"] = progress.reason
        task["verified"] = False
        task["issues"] = [issue.value for issue in report.issues]
        task["model_action"] = extraction.proposed_action if extraction else None
        task["final_action"] = action_for(decision).value
        task["updated_at"] = _now()
        store.update_task(task)
        return _view(store, task)

    checked = _verify(browser, workbook_path, job, filename)
    verified = progress.outcome == "READY_TO_VERIFY" and checked["ok"]
    _audit(
        store,
        task["id"],
        "cua_verify",
        "verified" if verified else "unverified",
        {
            "mismatches": checked["mismatches"],
            "reason": progress.reason,
            "rows": len(checked["rows"]),
            "plan": _plan_snapshot(job, progress),
        },
        on_event,
    )
    text = explain(decision, report)
    if progress.reason:
        text = f"{text} {progress.reason}"
    if not verified:
        text = f"{text} Independent {_verify_name(browser)} verification did not pass."
        if checked["mismatches"]:
            text = f"{text} {' '.join(checked['mismatches'])}"
    failed = progress.outcome in {"FAILED", "UNVERIFIED"} or not checked["ok"]
    task["status"] = "failed" if failed else _settle(decision, True, False, False)
    task["decision"] = decision.value
    task["explanation"] = text
    task["invoice_id"] = None
    task["verified"] = verified
    task["issues"] = [issue.value for issue in report.issues]
    task["model_action"] = extraction.proposed_action if extraction else None
    task["final_action"] = action_for(decision).value
    task["updated_at"] = _now()
    store.update_task(task)
    if verified and job.invoice_id:
        remember_lesson(store.conn, job.invoice_id, task["status"], store.audit_for(task["id"]))
    return _view(store, task)


def _write_checkpoint(workbook_path: Path, task: dict, job: Job, payload: dict) -> None:
    current = load_session(workbook_path, task["id"]) or {}
    plan = payload.get("plan") or {}
    current["checkpoint"] = {
        "task_id": task["id"],
        "goal": plan.get("task_goal"),
        "current_goal": plan.get("current_goal"),
        "completed_goals": plan.get("completed") or [],
        "last_confirmed_action": payload.get("action"),
        "retry_count": plan.get("retry_count") or 0,
        "intended_row": job.intended,
        "pending_question": plan.get("pending_question"),
        "evidence": plan.get("evidence") or {},
        "status": "running",
    }
    current["invoice_id"] = job.invoice_id
    save_session(workbook_path, task["id"], current)


def _verify(browser, workbook_path: Path, job: Job, filename: str) -> dict:
    kwargs = {
        "invoice_id": job.invoice_id,
        "expect_row": job.intended is not None,
        "intended": job.intended,
        "source_pdf": filename,
    }
    read_back = getattr(browser, "read_back", None)
    if read_back is not None:
        return verify_rows(read_back(), **kwargs)
    return verify_ledger(workbook_path, **kwargs)


def _verify_name(browser) -> str:
    if getattr(browser, "read_back", None) is not None:
        return "ledger"
    return "workbook"


def _live_chooser(settings: Settings | None) -> ActionChooser | None:
    if settings is None or not settings.groq_api_key or settings.extractor == "template":
        return None
    return ActionChooser(settings)


def _job(filename: str, extraction: Extraction | None, report: MatchReport, decision: Decision, vendor_choice: str | None) -> Job:
    issues = set(report.issues)
    if extraction is None or Issue.POOR_SCAN in issues or Issue.MISSING_REQUIRED_FIELDS in issues:
        return Job(
            invoice_id="",
            source_pdf=filename,
            vendor_query="",
            vendor_expect="skip",
            po_query="",
            po_expect="none",
            grn_query="",
            grn_expect="none",
            intended=None,
        )
    clarify = Issue.VENDOR_AMBIGUOUS in issues and not vendor_choice
    if Issue.VENDOR_AMBIGUOUS in issues and not vendor_choice:
        vendor_expect = "many"
    elif Issue.VENDOR_UNKNOWN in issues:
        vendor_expect = "none"
    else:
        vendor_expect = "one"
    intended = None
    if _should_persist(decision, report.issues) and not clarify and _amounts(report):
        intended = {
            "Invoice ID": extraction.invoice_number or "",
            "Vendor": report.vendor_name or extraction.vendor_name or "",
            "Tax ID": extraction.vendor_tax_id or "",
            "Invoice Date": extraction.invoice_date or "",
            "PO ID": extraction.po_number or "",
            "GRN ID": extraction.grn_number or "",
            "Subtotal": cents_to_str(report.subtotal_cents or 0),
            "Tax Rate": bps_to_str(report.tax_rate_bps or 0),
            "Tax Amount": cents_to_str(report.tax_cents or 0),
            "Total": cents_to_str(report.total_cents or 0),
            "Status": "NEEDS_APPROVAL" if decision is Decision.READY else "NEEDS_REVIEW",
            "Exception": _exceptions(report.issues),
            "Source PDF": filename,
            "Processed At": _now(),
        }
    return Job(
        invoice_id=extraction.invoice_number or "",
        source_pdf=filename,
        vendor_query=report.vendor_name or extraction.vendor_name or "",
        vendor_expect=vendor_expect,
        po_query=extraction.po_number or "",
        po_expect="none" if Issue.PO_MISSING in issues or not extraction.po_number else "one",
        grn_query=extraction.grn_number or "",
        grn_expect="none" if Issue.GRN_MISSING in issues or not extraction.grn_number else "one",
        intended=intended,
        duplicate_fixture=Issue.DUPLICATE_INVOICE in issues,
        needs_clarification=clarify,
        candidates=list(report.candidates),
    )


def _exceptions(issues: list[Issue]) -> str:
    codes: list[str] = []
    for issue in issues:
        code = ISSUE_CODES.get(issue.value)
        if code and code not in codes:
            codes.append(code)
    return ",".join(codes)


def _amounts(report: MatchReport) -> bool:
    return None not in {report.subtotal_cents, report.tax_cents, report.total_cents, report.tax_rate_bps}


def _ensure_task(store, existing, instruction, filename, key) -> dict:
    now = _now()
    if existing is None:
        task = {
            "id": str(uuid.uuid4()),
            "instruction": instruction,
            "filename": filename,
            "status": "running",
            "decision": None,
            "explanation": "",
            "idempotency_key": key,
            "invoice_id": None,
            "verified": False,
            "issues": [],
            "model_action": None,
            "final_action": None,
            "created_at": now,
            "updated_at": now,
        }
        store.insert_task(task)
        return task
    existing["status"] = "running"
    existing["updated_at"] = now
    store.update_task(existing)
    return existing


def _audit(store, task_id, step, outcome, detail, on_event) -> None:
    event = store.add_audit(task_id, step, outcome, detail)
    if on_event is not None:
        on_event(event)


def _view(store, task: dict) -> dict:
    return {
        "id": task["id"],
        "instruction": task["instruction"],
        "filename": task["filename"],
        "status": task["status"],
        "decision": task["decision"],
        "explanation": task["explanation"],
        "invoice_id": task["invoice_id"],
        "verified": bool(task["verified"]),
        "issues": task["issues"],
        "model_action": task["model_action"],
        "final_action": task["final_action"],
        "audit": store.audit_for(task["id"]),
        "usage": None,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
