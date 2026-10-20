import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Never

from app.accounting import AccountingStore
from app.extractor import Extraction
from app.money import decimal_to_cents, decimal_to_milli, money, percent_to_bps
from app.schemas import Action, Decision, Issue

REVIEW_ISSUES = {
    Issue.VENDOR_AMBIGUOUS,
    Issue.VENDOR_UNKNOWN,
    Issue.VENDOR_PO_CONFLICT,
    Issue.DUPLICATE_INVOICE,
    Issue.QUANTITY_MISMATCH,
    Issue.PRICE_MISMATCH,
    Issue.TAX_ARITHMETIC,
    Issue.LINE_UNMATCHED,
}
MISSING_ISSUES = {
    Issue.PO_MISSING,
    Issue.MISSING_REQUIRED_FIELDS,
    Issue.POOR_SCAN,
}


@dataclass
class MatchReport:
    vendor_status: str
    vendor_id: int | None
    vendor_name: str | None
    candidates: list[str] = field(default_factory=list)
    invoice_number: str | None = None
    po_number: str | None = None
    grn_number: str | None = None
    duplicate_id: int | None = None
    issues: list[Issue] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)
    subtotal_cents: int | None = None
    tax_rate_bps: int | None = None
    tax_cents: int | None = None
    total_cents: int | None = None
    currency: str = "INR"
    invoice_date: str | None = None


def match_invoice(
    store: AccountingStore,
    extraction: Extraction,
    own_key: str | None = None,
    vendor_choice: str | None = None,
) -> MatchReport:
    report = MatchReport(
        vendor_status="unknown",
        vendor_id=None,
        vendor_name=extraction.vendor_name,
        currency=extraction.currency or "INR",
        invoice_date=extraction.invoice_date,
        invoice_number=extraction.invoice_number,
        po_number=extraction.po_number,
        grn_number=extraction.grn_number,
    )
    _match_vendor(store, extraction, report, vendor_choice)
    _match_documents(store, extraction, report)
    _check_lines(extraction, report, store)
    _check_arithmetic(extraction, report)
    if report.vendor_id is not None and extraction.invoice_number:
        existing = store.find_invoice(report.vendor_id, extraction.invoice_number)
        if existing is not None and existing["idempotency_key"] != own_key:
            report.duplicate_id = int(existing["id"])
            report.issues.append(Issue.DUPLICATE_INVOICE)
            report.details[Issue.DUPLICATE_INVOICE.value] = (
                f"{extraction.invoice_number} for {existing['vendor_name']} "
                f"is already invoice {existing['id']}."
            )
    report.issues = _dedupe(report.issues)
    return report


def decide(issues: list[Issue]) -> Decision:
    present = set(issues)
    if present & REVIEW_ISSUES:
        return Decision.NEEDS_REVIEW
    if present & MISSING_ISSUES:
        return Decision.MISSING_INFORMATION
    if Issue.GRN_MISSING in present:
        return Decision.NEEDS_REVIEW
    if present:
        return Decision.NEEDS_REVIEW
    return Decision.READY


def action_for(decision: Decision) -> Action:
    match decision:
        case Decision.READY:
            return Action.POST_INVOICE
        case Decision.NEEDS_REVIEW:
            return Action.HOLD_FOR_REVIEW
        case Decision.MISSING_INFORMATION:
            return Action.REQUEST_CLARIFICATION
        case _:
            _never: Never = decision
            raise RuntimeError(_never)


def explain(decision: Decision, report: MatchReport, missing_fields: tuple[str, ...] = ()) -> str:
    sentences = [_decision_sentence(decision)]
    if missing_fields:
        sentences.append("Missing or invalid fields: " + ", ".join(missing_fields) + ".")
    for issue in report.issues:
        detail = report.details.get(issue.value)
        sentences.append(detail or _fallback_sentence(issue))
    if report.subtotal_cents is not None and report.tax_cents is not None and report.total_cents is not None:
        sentences.append(
            "Document amounts recorded as "
            f"subtotal {_cents(report.subtotal_cents)}, "
            f"tax {_cents(report.tax_cents)}, "
            f"total {_cents(report.total_cents)}."
        )
    return " ".join(sentences)


def _decision_sentence(decision: Decision) -> str:
    match decision:
        case Decision.READY:
            return "The invoice matches the purchase order and goods receipt, so it can be posted for approval."
        case Decision.NEEDS_REVIEW:
            return "The invoice is held for a person to review."
        case Decision.MISSING_INFORMATION:
            return "The invoice cannot be posted because required information is missing."
        case _:
            _never: Never = decision
            raise RuntimeError(_never)


def _fallback_sentence(issue: Issue) -> str:
    match issue:
        case Issue.VENDOR_AMBIGUOUS:
            return "The vendor name matches more than one vendor."
        case Issue.VENDOR_UNKNOWN:
            return "The vendor is not on the vendor list."
        case Issue.VENDOR_PO_CONFLICT:
            return "The vendor does not own the cited purchase order."
        case Issue.PO_MISSING:
            return "No purchase order supports this invoice."
        case Issue.GRN_MISSING:
            return "No goods receipt supports this invoice."
        case Issue.DUPLICATE_INVOICE:
            return "This vendor and invoice number already exist."
        case Issue.QUANTITY_MISMATCH:
            return "A billed quantity does not match the goods receipt."
        case Issue.PRICE_MISMATCH:
            return "A unit price does not match the purchase order."
        case Issue.TAX_ARITHMETIC:
            return "Tax or total arithmetic does not check out."
        case Issue.LINE_UNMATCHED:
            return "An invoice line has no purchase-order or goods-receipt line."
        case Issue.MISSING_REQUIRED_FIELDS:
            return "Required invoice fields are missing or invalid."
        case Issue.POOR_SCAN:
            return "The scan is too poor to read. A clearer copy is needed."
        case _:
            _never: Never = issue
            raise RuntimeError(_never)


def _match_vendor(
    store: AccountingStore,
    extraction: Extraction,
    report: MatchReport,
    vendor_choice: str | None = None,
) -> None:
    vendors = store.list_vendors()
    status, hits = _vendor_hits(vendors, extraction.vendor_name, extraction.vendor_tax_id)
    report.vendor_status = status
    report.candidates = [vendor["name"] for vendor in hits]
    if vendor_choice:
        _apply_vendor_choice(vendors, hits, vendor_choice, report)
        return
    match status:
        case "matched":
            report.vendor_id = int(hits[0]["id"])
            report.vendor_name = hits[0]["name"]
        case "ambiguous":
            report.issues.append(Issue.VENDOR_AMBIGUOUS)
            report.details[Issue.VENDOR_AMBIGUOUS.value] = (
                f"Vendor {extraction.vendor_name!r} matches {', '.join(report.candidates)}."
            )
        case "unknown":
            report.issues.append(Issue.VENDOR_UNKNOWN)
            report.details[Issue.VENDOR_UNKNOWN.value] = (
                f"Vendor {extraction.vendor_name or 'blank'!r} is not on the vendor list."
            )
        case _:
            report.issues.append(Issue.VENDOR_UNKNOWN)


def _apply_vendor_choice(
    vendors: list[dict],
    hits: list[dict],
    vendor_choice: str,
    report: MatchReport,
) -> None:
    allowed = {_norm(vendor["name"]) for vendor in hits} or set()
    chosen = [vendor for vendor in vendors if _norm(vendor["name"]) == _norm(vendor_choice)]
    if len(chosen) == 1 and _norm(chosen[0]["name"]) in allowed:
        report.vendor_status = "matched"
        report.vendor_id = int(chosen[0]["id"])
        report.vendor_name = chosen[0]["name"]
        report.details["vendor_choice"] = chosen[0]["name"]
        return
    report.vendor_status = "unknown"
    report.vendor_id = None
    report.issues.append(Issue.VENDOR_UNKNOWN)
    report.details[Issue.VENDOR_UNKNOWN.value] = (
        f"Vendor choice {vendor_choice!r} is not one of the matching vendors."
    )


def _vendor_hits(
    vendors: list[dict], name: str | None, tax_id: str | None
) -> tuple[str, list[dict]]:
    if tax_id:
        wanted = _tax(tax_id)
        hits = [vendor for vendor in vendors if _tax(vendor["tax_id"]) == wanted]
        if len(hits) == 1:
            return "matched", hits
        if len(hits) > 1:
            return "ambiguous", hits
    normalized = _norm(name or "")
    if not normalized:
        return "unknown", []
    exact = []
    for vendor in vendors:
        names = [_norm(vendor["name"]), *(_norm(alias) for alias in vendor["aliases"])]
        if normalized in names:
            exact.append(vendor)
    if len(exact) == 1:
        return "matched", exact
    if len(exact) > 1:
        return "ambiguous", exact
    tokens = set(normalized.split())
    partial = []
    for vendor in vendors:
        vendor_tokens = set(_norm(vendor["name"]).split())
        if tokens and tokens <= vendor_tokens:
            partial.append(vendor)
    if len(partial) == 1:
        return "matched", partial
    if len(partial) > 1:
        return "ambiguous", partial
    return "unknown", []


def _match_documents(store: AccountingStore, extraction: Extraction, report: MatchReport) -> None:
    po = store.purchase_order(extraction.po_number) if extraction.po_number else None
    if po is None:
        report.issues.append(Issue.PO_MISSING)
        if extraction.po_number:
            report.details[Issue.PO_MISSING.value] = (
                f"Purchase order {extraction.po_number} is not in the books."
            )
        else:
            report.details[Issue.PO_MISSING.value] = "The invoice does not cite a purchase order."
    elif report.vendor_id is not None and int(po["vendor_id"]) != report.vendor_id:
        report.issues.append(Issue.VENDOR_PO_CONFLICT)
        report.details[Issue.VENDOR_PO_CONFLICT.value] = (
            f"{extraction.po_number} belongs to a different vendor."
        )
    grn = store.goods_receipt(extraction.grn_number) if extraction.grn_number else None
    if grn is None:
        report.issues.append(Issue.GRN_MISSING)
        if extraction.grn_number:
            report.details[Issue.GRN_MISSING.value] = (
                f"Goods receipt {extraction.grn_number} is not in the books."
            )
        else:
            report.details[Issue.GRN_MISSING.value] = "The invoice does not cite a goods receipt."
    report.details["po"] = json.dumps({"found": po is not None})
    report.details["grn"] = json.dumps({"found": grn is not None})


def _check_lines(extraction: Extraction, report: MatchReport, store: AccountingStore) -> None:
    po = store.purchase_order(extraction.po_number) if extraction.po_number else None
    grn = store.goods_receipt(extraction.grn_number) if extraction.grn_number else None
    po_lines = {line["sku"].casefold(): line for line in po["lines"]} if po else {}
    grn_lines = {line["sku"].casefold(): line for line in grn["lines"]} if grn else {}
    subtotal = 0
    for line in extraction.line_items:
        if line.quantity is None or line.unit_price is None:
            continue
        amount = line.line_total if line.line_total is not None else line.quantity * line.unit_price
        subtotal += decimal_to_cents(amount)
        sku = (line.sku or "").casefold()
        po_line = po_lines.get(sku)
        grn_line = grn_lines.get(sku)
        if po and po_line is None:
            report.issues.append(Issue.LINE_UNMATCHED)
            report.details[Issue.LINE_UNMATCHED.value] = (
                f"{line.sku or line.description} is not on {extraction.po_number}."
            )
        if grn and grn_line is None:
            report.issues.append(Issue.LINE_UNMATCHED)
            report.details[Issue.LINE_UNMATCHED.value] = (
                f"{line.sku or line.description} is not on {extraction.grn_number}."
            )
        if po_line is not None:
            billed = decimal_to_cents(line.unit_price)
            if billed != int(po_line["unit_price_cents"]):
                report.issues.append(Issue.PRICE_MISMATCH)
                report.details[Issue.PRICE_MISMATCH.value] = (
                    f"{line.sku} is billed at {line.unit_price} "
                    f"but {extraction.po_number} has unit price "
                    f"{_cents(int(po_line['unit_price_cents']))}."
                )
        if grn_line is not None:
            billed_qty = decimal_to_milli(line.quantity)
            received = int(grn_line["quantity_milli"])
            if billed_qty != received:
                report.issues.append(Issue.QUANTITY_MISMATCH)
                report.details[Issue.QUANTITY_MISMATCH.value] = (
                    f"{line.sku} bills quantity {line.quantity} "
                    f"but {extraction.grn_number} received {_milli(received)}."
                )
    if extraction.line_items:
        report.subtotal_cents = (
            decimal_to_cents(extraction.subtotal) if extraction.subtotal is not None else subtotal
        )


def _check_arithmetic(extraction: Extraction, report: MatchReport) -> None:
    if extraction.subtotal is None or extraction.tax_amount is None or extraction.total is None:
        report.issues.append(Issue.TAX_ARITHMETIC)
        report.details[Issue.TAX_ARITHMETIC.value] = "Subtotal, tax, or total is missing."
        return
    subtotal = money(extraction.subtotal)
    stated_tax = money(extraction.tax_amount)
    stated_total = money(extraction.total)
    rate = extraction.tax_rate_percent if extraction.tax_rate_percent is not None else Decimal("0")
    if Decimal("0") < rate < Decimal("1"):
        rate = rate * Decimal(100)
    expected_tax = money(subtotal * rate / Decimal(100))
    expected_total = money(subtotal + expected_tax)
    line_sum = Decimal("0")
    for line in extraction.line_items:
        if line.quantity is None or line.unit_price is None:
            continue
        line_sum += line.line_total if line.line_total is not None else line.quantity * line.unit_price
    problems: list[str] = []
    if extraction.line_items and money(line_sum) != subtotal:
        problems.append(
            f"line amounts sum to {money(line_sum)} but the subtotal is {subtotal}"
        )
    if stated_tax != expected_tax:
        problems.append(
            f"tax {stated_tax} is not {rate}% of {subtotal} ({expected_tax})"
        )
    if stated_total != expected_total:
        problems.append(f"total {stated_total} is not subtotal plus computed tax ({expected_total})")
    if stated_total != money(subtotal + stated_tax):
        problems.append(f"total {stated_total} is not subtotal plus stated tax")
    report.subtotal_cents = decimal_to_cents(subtotal)
    report.tax_cents = decimal_to_cents(stated_tax)
    report.total_cents = decimal_to_cents(stated_total)
    report.tax_rate_bps = percent_to_bps(rate)
    if problems:
        report.issues.append(Issue.TAX_ARITHMETIC)
        report.details[Issue.TAX_ARITHMETIC.value] = "Arithmetic check failed: " + "; ".join(problems) + "."


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen: set[Issue] = set()
    unique: list[Issue] = []
    for issue in issues:
        if issue in seen:
            continue
        seen.add(issue)
        unique.append(issue)
    return unique


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _tax(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def _milli(milli: int) -> str:
    whole, frac = divmod(abs(milli), 1000)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:03d}".rstrip("0")
