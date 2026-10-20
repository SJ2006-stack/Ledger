import contextvars
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Never

from app.accounting import (
    AccountingStore,
    DuplicateInvoice,
    InvoiceDraft,
    LineDraft,
    SaveFailed,
)
from app.documents import store_task_pdf
from app.extractor import Extraction, ExtractionError, Extractor, validate_extraction
from app.matcher import MatchReport, action_for, decide, explain, match_invoice
from app.money import decimal_to_cents, decimal_to_milli
from app.parser import parse_pdf
from app.schemas import Action, Decision, Issue
from app.verifier import Intention, verify

_audit_listener: contextvars.ContextVar[Callable[[dict], None] | None] = contextvars.ContextVar(
    "audit_listener",
    default=None,
)


class Orchestrator:
    def __init__(self, store: AccountingStore, extractor: Extractor) -> None:
        self.store = store
        self.extractor = extractor

    def run(
        self,
        *,
        instruction: str,
        filename: str,
        pdf_bytes: bytes,
        idempotency_key: str,
        lose_response_after_save: bool = False,
        on_event: Callable[[dict], None] | None = None,
    ) -> dict:
        token = _audit_listener.set(on_event)
        try:
            return self._run(
                instruction=instruction,
                filename=filename,
                pdf_bytes=pdf_bytes,
                idempotency_key=idempotency_key,
                lose_response_after_save=lose_response_after_save,
            )
        finally:
            _audit_listener.reset(token)

    def _run(
        self,
        *,
        instruction: str,
        filename: str,
        pdf_bytes: bytes,
        idempotency_key: str,
        lose_response_after_save: bool = False,
    ) -> dict:
        existing = self.store.task_by_key(idempotency_key)
        if existing is not None and existing["status"] != "failed":
            return self._view(existing)
        task_id = existing["id"] if existing else str(uuid.uuid4())
        now = _now()
        task = {
            "id": task_id,
            "instruction": instruction,
            "filename": filename,
            "status": "running",
            "decision": None,
            "explanation": "",
            "idempotency_key": idempotency_key,
            "invoice_id": None,
            "verified": False,
            "issues": [],
            "model_action": None,
            "final_action": None,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        if existing is None:
            self.store.insert_task(task)
        else:
            self.store.update_task(task)
        store_task_pdf(task_id, pdf_bytes)
        self._audit(task_id, "received", "accepted", {"instruction": instruction})
        return self._execute(task, pdf_bytes, lose_response_after_save)

    def _execute(self, task: dict, pdf_bytes: bytes, lose_response_after_save: bool) -> dict:
        task_id = task["id"]
        parsed = parse_pdf(pdf_bytes)
        self._audit(
            task_id,
            "parse_document",
            parsed.quality,
            {
                "pages": parsed.pages,
                "used_ocr": parsed.used_ocr,
                "ocr_confidence": parsed.ocr_confidence,
                "characters": len(parsed.text),
            },
        )
        if parsed.quality == "poor":
            self._audit(task_id, "extract_fields", "retry", {"reason": "poor_scan"})
            second = parse_pdf(pdf_bytes)
            self._audit(
                task_id,
                "extract_fields",
                "request_clarification",
                {"reason": "poor_scan", "second_quality": second.quality},
            )
            report = MatchReport(vendor_status="unknown", vendor_id=None, vendor_name=None)
            report.issues.append(Issue.POOR_SCAN)
            return self._finish(
                task,
                decision=Decision.MISSING_INFORMATION,
                report=report,
                missing_fields=(),
                model_action=None,
                invoice_id=None,
                expect_row=False,
                save_failed=False,
                response_lost=False,
                usage=None,
            )

        try:
            extraction = self.extractor.extract(parsed.text, task["filename"] or "invoice.pdf")
        except ExtractionError as exc:
            self._audit(
                task_id,
                "extract_fields",
                "failed",
                {"error": str(exc), "thinking": exc.thinking},
            )
            task["status"] = "failed"
            task["explanation"] = str(exc)
            task["updated_at"] = _now()
            self.store.update_task(task)
            return self._view(task)

        self._audit(
            task_id,
            "extract_fields",
            "extracted",
            {
                "model": extraction.model,
                "invoice_number": extraction.invoice_number,
                "vendor_name": extraction.vendor_name,
                "proposed_action": extraction.proposed_action,
                "snapshot": _snapshot(extraction),
                "model_note": extraction.model_note,
                "thinking": extraction.thinking,
                "thoughts_tokens": extraction.thoughts_tokens,
                "evidence": [
                    {"field": item.field, "quote": item.quote} for item in extraction.evidence
                ],
                "input_tokens": extraction.input_tokens,
                "output_tokens": extraction.output_tokens,
                "estimated_cost_usd": extraction.estimated_cost_usd,
            },
        )
        validation = validate_extraction(extraction)
        self._audit(
            task_id,
            "validate_fields",
            "ok" if validation.ok else "rejected",
            {"missing": list(validation.missing)},
        )
        if not validation.ok:
            report = MatchReport(
                vendor_status="unknown",
                vendor_id=None,
                vendor_name=extraction.vendor_name,
            )
            report.issues.append(Issue.MISSING_REQUIRED_FIELDS)
            return self._finish(
                task,
                decision=Decision.MISSING_INFORMATION,
                report=report,
                missing_fields=validation.missing,
                model_action=extraction.proposed_action,
                invoice_id=None,
                expect_row=False,
                save_failed=False,
                response_lost=False,
                usage=_usage(extraction),
            )

        report = match_invoice(self.store, extraction, task["idempotency_key"])
        self._audit(
            task_id,
            "match_records",
            report.vendor_status,
            {"issues": [issue.value for issue in report.issues], "vendor_id": report.vendor_id},
        )
        decision = decide(report.issues)
        final_action = action_for(decision)
        self._audit(
            task_id,
            "decide",
            decision.value,
            {
                "final_action": final_action.value,
                "model_action": extraction.proposed_action,
                "overridden": _overridden(extraction.proposed_action, final_action),
            },
        )
        return self._persist(
            task,
            extraction,
            report,
            decision,
            final_action,
            lose_response_after_save,
        )

    def _persist(
        self,
        task: dict,
        extraction: Extraction,
        report: MatchReport,
        decision: Decision,
        final_action: Action,
        lose_response_after_save: bool,
    ) -> dict:
        task_id = task["id"]
        explanation = explain(decision, report)
        if decision is Decision.READY and not _amounts_ready(report):
            report.issues.append(Issue.TAX_ARITHMETIC)
            decision = Decision.NEEDS_REVIEW
            final_action = action_for(decision)
            explanation = explain(decision, report)
        if not _should_persist(decision, report.issues) or not _amounts_ready(report):
            self._audit(task_id, "create_invoice", "skipped", {"reason": final_action.value})
            return self._finish(
                task,
                decision=decision,
                report=report,
                missing_fields=(),
                model_action=extraction.proposed_action,
                invoice_id=None,
                expect_row=False,
                save_failed=False,
                response_lost=False,
                usage=_usage(extraction),
                explanation=explanation,
            )

        draft = _draft(task, extraction, report, decision, explanation)
        try:
            saved, created = self.store.create_invoice(draft)
        except DuplicateInvoice as exc:
            report.issues.append(Issue.DUPLICATE_INVOICE)
            report.duplicate_id = exc.existing_id
            decision = Decision.NEEDS_REVIEW
            self._audit(task_id, "create_invoice", "duplicate", {"existing_id": exc.existing_id})
            return self._finish(
                task,
                decision=decision,
                report=report,
                missing_fields=(),
                model_action=extraction.proposed_action,
                invoice_id=None,
                expect_row=False,
                save_failed=False,
                response_lost=False,
                usage=_usage(extraction),
            )
        except SaveFailed as exc:
            self._audit(task_id, "create_invoice", "save_failed", {"error": str(exc)})
            return self._finish(
                task,
                decision=decision,
                report=report,
                missing_fields=(),
                model_action=extraction.proposed_action,
                invoice_id=None,
                expect_row=False,
                save_failed=True,
                response_lost=False,
                usage=_usage(extraction),
                explanation=f"{explanation} Save failed: {exc}",
            )

        if lose_response_after_save and created:
            self._audit(task_id, "create_invoice", "response_lost", {"invoice_id": saved["id"]})
            return self._finish(
                task,
                decision=decision,
                report=report,
                missing_fields=(),
                model_action=extraction.proposed_action,
                invoice_id=None,
                expect_row=True,
                save_failed=False,
                response_lost=True,
                usage=_usage(extraction),
                explanation=f"{explanation} The save succeeded but the response was lost.",
            )

        self._audit(
            task_id,
            "create_invoice",
            "created" if created else "existing",
            {"invoice_id": saved["id"], "status": saved["status"]},
        )
        return self._finish(
            task,
            decision=decision,
            report=report,
            missing_fields=(),
            model_action=extraction.proposed_action,
            invoice_id=int(saved["id"]),
            expect_row=True,
            save_failed=False,
            response_lost=False,
            usage=_usage(extraction),
            explanation=explanation,
        )

    def _finish(
        self,
        task: dict,
        *,
        decision: Decision,
        report: MatchReport,
        missing_fields: tuple[str, ...],
        model_action: str | None,
        invoice_id: int | None,
        expect_row: bool,
        save_failed: bool,
        response_lost: bool,
        usage: dict | None,
        explanation: str | None = None,
    ) -> dict:
        checked = verify(
            self.store,
            Intention(
                idempotency_key=task["idempotency_key"],
                expect_row=expect_row,
                vendor_id=report.vendor_id,
                invoice_number=report.invoice_number,
                po_number=report.po_number,
                status=_stored_status(decision) if expect_row else None,
                subtotal_cents=report.subtotal_cents if expect_row else None,
                tax_cents=report.tax_cents if expect_row else None,
                total_cents=report.total_cents if expect_row else None,
            ),
        )
        self._audit(
            task["id"],
            "verify_persisted",
            "passed" if checked.ok else "failed",
            {
                "mismatches": checked.mismatches,
                "observed_id": checked.observed_id,
                "observed_status": checked.observed_status,
                "expect_row": expect_row,
            },
        )
        text = explanation or explain(decision, report, missing_fields)
        if not checked.ok:
            text = f"{text} Independent verification failed: {', '.join(checked.mismatches)}."
        task["decision"] = decision.value
        task["status"] = _settle(decision, checked.ok, save_failed, response_lost)
        task["explanation"] = text
        task["invoice_id"] = invoice_id if not response_lost else None
        task["verified"] = checked.ok and not save_failed and not response_lost
        task["issues"] = [issue.value for issue in report.issues]
        task["model_action"] = model_action
        task["final_action"] = action_for(decision).value
        task["updated_at"] = _now()
        self.store.update_task(task)
        view = self._view(task)
        view["usage"] = usage
        return view

    def _view(self, task: dict) -> dict:
        audit = self.store.audit_for(task["id"])
        usage = _usage_from_audit(audit)
        return {
            "id": task["id"],
            "instruction": task["instruction"],
            "filename": task["filename"],
            "status": task["status"],
            "decision": task["decision"],
            "explanation": task["explanation"],
            "invoice_id": task["invoice_id"],
            "verified": task["verified"],
            "issues": task["issues"],
            "model_action": task["model_action"],
            "final_action": task["final_action"],
            "audit": audit,
            "usage": usage,
        }

    def _audit(self, task_id: str, step: str, outcome: str, detail: dict) -> None:
        event = self.store.add_audit(task_id, step, outcome, detail)
        listener = _audit_listener.get()
        if listener is not None:
            listener(event)


def _should_persist(decision: Decision, issues: list[Issue]) -> bool:
    blocked = {
        Issue.DUPLICATE_INVOICE,
        Issue.POOR_SCAN,
        Issue.MISSING_REQUIRED_FIELDS,
        Issue.VENDOR_AMBIGUOUS,
        Issue.VENDOR_UNKNOWN,
    }
    if set(issues) & blocked:
        return False
    match decision:
        case Decision.READY | Decision.NEEDS_REVIEW:
            return True
        case Decision.MISSING_INFORMATION:
            return Issue.PO_MISSING in issues
        case _:
            _never: Never = decision
            raise RuntimeError(_never)


def _stored_status(decision: Decision) -> str:
    match decision:
        case Decision.READY:
            return "pending_approval"
        case Decision.NEEDS_REVIEW | Decision.MISSING_INFORMATION:
            return "needs_review"
        case _:
            _never: Never = decision
            raise RuntimeError(_never)


def _settle(decision: Decision, verification_ok: bool, save_failed: bool, response_lost: bool) -> str:
    if save_failed or response_lost:
        return "failed"
    match decision:
        case Decision.READY:
            return "completed" if verification_ok else "failed"
        case Decision.NEEDS_REVIEW:
            return "needs_review" if verification_ok else "failed"
        case Decision.MISSING_INFORMATION:
            return "missing_information" if verification_ok else "failed"
        case _:
            _never: Never = decision
            raise RuntimeError(_never)


def _draft(
    task: dict,
    extraction: Extraction,
    report: MatchReport,
    decision: Decision,
    explanation: str,
) -> InvoiceDraft:
    lines = []
    for line in extraction.line_items:
        if line.quantity is None or line.unit_price is None:
            continue
        amount = line.line_total if line.line_total is not None else line.quantity * line.unit_price
        lines.append(
            LineDraft(
                sku=line.sku or "",
                description=line.description,
                quantity_milli=decimal_to_milli(line.quantity),
                unit_price_cents=decimal_to_cents(line.unit_price),
                line_total_cents=decimal_to_cents(amount),
            )
        )
    if report.subtotal_cents is None or report.tax_cents is None or report.total_cents is None:
        raise SaveFailed("Refusing to save an invoice without amounts.")
    return InvoiceDraft(
        vendor_id=report.vendor_id,
        vendor_name=report.vendor_name or extraction.vendor_name or "",
        invoice_number=extraction.invoice_number or "",
        po_number=extraction.po_number,
        grn_number=extraction.grn_number,
        invoice_date=extraction.invoice_date,
        currency=report.currency,
        subtotal_cents=report.subtotal_cents,
        tax_rate_bps=report.tax_rate_bps or 0,
        tax_cents=report.tax_cents,
        total_cents=report.total_cents,
        status=_stored_status(decision),
        idempotency_key=task["idempotency_key"],
        task_id=task["id"],
        decision=decision.value,
        explanation=explanation,
        lines=tuple(lines),
    )


def _amounts_ready(report: MatchReport) -> bool:
    return None not in {
        report.subtotal_cents,
        report.tax_cents,
        report.total_cents,
        report.tax_rate_bps,
    }


def _overridden(model_action: str | None, final_action: Action) -> bool:
    return bool(model_action) and model_action != final_action.value


def _snapshot(extraction: Extraction) -> dict:
    line = extraction.line_items[0] if extraction.line_items else None
    return {
        "vendor_name": extraction.vendor_name,
        "vendor_tax_id": extraction.vendor_tax_id,
        "invoice_number": extraction.invoice_number,
        "po_number": extraction.po_number,
        "grn_number": extraction.grn_number,
        "quantity": str(line.quantity) if line and line.quantity is not None else None,
        "unit_price": str(line.unit_price) if line and line.unit_price is not None else None,
        "tax_amount": str(extraction.tax_amount) if extraction.tax_amount is not None else None,
        "total": str(extraction.total) if extraction.total is not None else None,
    }


def _usage(extraction: Extraction) -> dict:
    return {
        "model": "recorded-on-audit",
        "input_tokens": extraction.input_tokens,
        "output_tokens": extraction.output_tokens,
        "estimated_cost_usd": extraction.estimated_cost_usd,
    }


def _usage_from_audit(audit: list[dict]) -> dict | None:
    for event in audit:
        if event["step"] == "extract_fields" and event["outcome"] == "extracted":
            detail = event["detail"]
            if not detail.get("input_tokens") and not detail.get("output_tokens"):
                return None
            return {
                "input_tokens": detail.get("input_tokens", 0),
                "output_tokens": detail.get("output_tokens", 0),
                "estimated_cost_usd": detail.get("estimated_cost_usd", 0),
            }
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
