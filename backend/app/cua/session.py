import json
from pathlib import Path

from app.extractor import ExtractedLine, Extraction
from app.money import parse_decimal


def session_path(workbook: Path, task_id: str) -> Path:
    return workbook.parent / "cua_sessions" / f"{task_id}.json"


def save_session(workbook: Path, task_id: str, payload: dict) -> None:
    path = session_path(workbook, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def load_session(workbook: Path, task_id: str) -> dict | None:
    path = session_path(workbook, task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def extraction_payload(extraction: Extraction) -> dict:
    return {
        "vendor_name": extraction.vendor_name,
        "vendor_tax_id": extraction.vendor_tax_id,
        "invoice_number": extraction.invoice_number,
        "invoice_date": extraction.invoice_date,
        "po_number": extraction.po_number,
        "grn_number": extraction.grn_number,
        "currency": extraction.currency,
        "subtotal": _text(extraction.subtotal),
        "tax_rate_percent": _text(extraction.tax_rate_percent),
        "tax_amount": _text(extraction.tax_amount),
        "total": _text(extraction.total),
        "proposed_action": extraction.proposed_action,
        "model_note": extraction.model_note,
        "model": extraction.model,
        "line_items": [
            {
                "description": line.description,
                "sku": line.sku,
                "quantity": _text(line.quantity),
                "unit_price": _text(line.unit_price),
                "line_total": _text(line.line_total),
            }
            for line in extraction.line_items
        ],
    }


def extraction_from_payload(payload: dict) -> Extraction:
    lines = tuple(
        ExtractedLine(
            description=item["description"],
            sku=item.get("sku"),
            quantity=parse_decimal(item.get("quantity")),
            unit_price=parse_decimal(item.get("unit_price")),
            line_total=parse_decimal(item.get("line_total")),
        )
        for item in payload.get("line_items") or []
    )
    return Extraction(
        vendor_name=payload.get("vendor_name"),
        vendor_tax_id=payload.get("vendor_tax_id"),
        invoice_number=payload.get("invoice_number"),
        invoice_date=payload.get("invoice_date"),
        po_number=payload.get("po_number"),
        grn_number=payload.get("grn_number"),
        currency=payload.get("currency"),
        line_items=lines,
        subtotal=parse_decimal(payload.get("subtotal")),
        tax_rate_percent=parse_decimal(payload.get("tax_rate_percent")),
        tax_amount=parse_decimal(payload.get("tax_amount")),
        total=parse_decimal(payload.get("total")),
        proposed_action=payload.get("proposed_action"),
        model_note=payload.get("model_note"),
        evidence=(),
        model=payload.get("model") or "",
    )


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
