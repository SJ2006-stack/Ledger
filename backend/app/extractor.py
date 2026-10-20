import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.config import INPUT_USD_PER_MILLION, OUTPUT_USD_PER_MILLION
from app.llm import generate_json, is_transient
from app.money import parse_decimal

COLUMN_RE = re.compile(
    r"^([A-Z0-9][A-Z0-9-]*)\n(.+)\n([0-9][0-9,]*(?:\.[0-9]+)?)\n([0-9][0-9,]*\.[0-9]+)\n([0-9][0-9,]*\.[0-9]+)\s*$",
    re.MULTILINE,
)
ITEM_RE = re.compile(
    r"^Item:\s*(.*?)\s*\|\s*(.*?)\s*\|\s*qty\s+([0-9.]+)\s*\|\s*unit\s+([0-9.]+)\s*\|\s*amount\s+([0-9.]+)\s*$",
    re.MULTILINE,
)
LABELS = {
    "vendor_tax_id": r"^Tax ID:[ \t]*(.*)$",
    "invoice_number": r"^Invoice Number:[ \t]*(.*)$",
    "invoice_date": r"^Invoice Date:[ \t]*(.*)$",
    "po_number": r"^Purchase Order:[ \t]*(.*)$",
    "grn_number": r"^Goods Receipt:[ \t]*(.*)$",
    "currency": r"^Currency:[ \t]*(.*)$",
    "subtotal": r"^Subtotal:[ \t]*(.*)$",
    "tax_rate_percent": r"^Tax Rate:[ \t]*(.*)$",
    "tax_amount": r"^Tax:[ \t]*(.*)$",
    "total": r"^Total:[ \t]*(.*)$",
}

_NEXT_IS_LABEL = re.compile(
    r"^(Tax ID|Invoice Number|Invoice Date|Purchase Order|Goods Receipt|Currency|Subtotal|Tax Rate|Tax|Total|Items|SKU|Description|Qty|Unit price|Amount|SUPPLIER DETAILS|BILL TO)\b",
    re.IGNORECASE,
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name": {"type": "string"},
        "vendor_tax_id": {"type": "string"},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string"},
        "po_number": {"type": "string"},
        "grn_number": {"type": "string"},
        "currency": {"type": "string"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "sku": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "line_total": {"type": "number"},
                },
                "required": ["description", "sku", "quantity", "unit_price", "line_total"],
            },
        },
        "subtotal": {"type": "number"},
        "tax_rate_percent": {"type": "number"},
        "tax_amount": {"type": "number"},
        "total": {"type": "number"},
        "proposed_action": {
            "type": "string",
            "enum": ["post_invoice", "hold_for_review", "request_clarification"],
        },
        "model_note": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["field", "quote"],
            },
        },
    },
    "required": [
        "vendor_name",
        "vendor_tax_id",
        "invoice_number",
        "invoice_date",
        "po_number",
        "grn_number",
        "currency",
        "line_items",
        "subtotal",
        "tax_rate_percent",
        "tax_amount",
        "total",
        "proposed_action",
        "model_note",
        "evidence",
    ],
}


class ExtractionError(Exception):
    def __init__(self, message: str, thinking: str = "") -> None:
        super().__init__(message)
        self.thinking = thinking


@dataclass(frozen=True)
class Evidence:
    field: str
    quote: str


@dataclass(frozen=True)
class ExtractedLine:
    description: str
    sku: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    line_total: Decimal | None


@dataclass(frozen=True)
class Extraction:
    vendor_name: str | None
    vendor_tax_id: str | None
    invoice_number: str | None
    invoice_date: str | None
    po_number: str | None
    grn_number: str | None
    currency: str | None
    line_items: tuple[ExtractedLine, ...]
    subtotal: Decimal | None
    tax_rate_percent: Decimal | None
    tax_amount: Decimal | None
    total: Decimal | None
    proposed_action: str | None
    model_note: str | None
    evidence: tuple[Evidence, ...]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking: str = ""
    thoughts_tokens: int = 0

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self.input_tokens * INPUT_USD_PER_MILLION
            + self.output_tokens * OUTPUT_USD_PER_MILLION
        ) / 1_000_000


@dataclass(frozen=True)
class Validation:
    ok: bool
    missing: tuple[str, ...]


class Extractor(Protocol):
    def extract(self, document_text: str, filename: str) -> Extraction:
        """Return invoice fields found in the document."""


class TemplateExtractor:
    """Deterministic reader for the synthetic invoice template. Not a model."""

    def extract(self, document_text: str, filename: str) -> Extraction:
        del filename
        vendor_name = _first_line(document_text)
        fields = {name: _label(document_text, pattern) for name, pattern in LABELS.items()}
        lines = []
        for match in ITEM_RE.finditer(document_text):
            lines.append(
                ExtractedLine(
                    sku=_blank(match.group(1)),
                    description=match.group(2).strip(),
                    quantity=parse_decimal(match.group(3)),
                    unit_price=parse_decimal(match.group(4)),
                    line_total=parse_decimal(match.group(5)),
                )
            )
        if not lines:
            for match in COLUMN_RE.finditer(document_text):
                lines.append(
                    ExtractedLine(
                        sku=_blank(match.group(1)),
                        description=match.group(2).strip(),
                        quantity=parse_decimal(match.group(3)),
                        unit_price=parse_decimal(match.group(4)),
                        line_total=parse_decimal(match.group(5)),
                    )
                )
        return Extraction(
            vendor_name=_blank(vendor_name),
            vendor_tax_id=_blank(fields["vendor_tax_id"]),
            invoice_number=_blank(fields["invoice_number"]),
            invoice_date=_blank(fields["invoice_date"]),
            po_number=_blank(fields["po_number"]),
            grn_number=_blank(fields["grn_number"]),
            currency=_blank(fields["currency"]),
            line_items=tuple(lines),
            subtotal=parse_decimal(fields["subtotal"]),
            tax_rate_percent=parse_decimal(_percent(fields["tax_rate_percent"])),
            tax_amount=parse_decimal(fields["tax_amount"]),
            total=parse_decimal(fields["total"]),
            proposed_action=None,
            model_note=None,
            evidence=(),
        )


class GroqExtractor:
    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ExtractionError("GROQ_API_KEY is not set.")
        self.model = model
        self._api_key = api_key

    def extract(self, document_text: str, filename: str) -> Extraction:
        prompt = _prompt(document_text, filename)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                reply = generate_json(
                    self._api_key,
                    self.model,
                    prompt,
                    max_tokens=4096,
                )
                return _from_payload(
                    reply.payload,
                    reply.input_tokens,
                    reply.output_tokens,
                    self.model,
                    "",
                    0,
                )
            except Exception as exc:
                last_error = exc
                if attempt < 2 and is_transient(exc):
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ExtractionError(f"Model request failed: {exc}") from exc
        raise ExtractionError(f"Model request failed: {last_error}")


# Kept for older imports / type checks.
GeminiExtractor = GroqExtractor


def validate_extraction(extraction: Extraction) -> Validation:
    missing: list[str] = []
    if not extraction.vendor_name:
        missing.append("vendor_name")
    if not extraction.invoice_number:
        missing.append("invoice_number")
    if not extraction.line_items:
        missing.append("line_items")
    for index, line in enumerate(extraction.line_items):
        if line.quantity is None or line.quantity <= 0:
            missing.append(f"line_{index}_quantity")
        if line.unit_price is None or line.unit_price < 0:
            missing.append(f"line_{index}_unit_price")
    return Validation(ok=not missing, missing=tuple(missing))


def _thought_text(response: object) -> str:
    chunks: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            if not getattr(part, "thought", False):
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n\n".join(chunks)


def _from_payload(
    payload: dict,
    input_tokens: int,
    output_tokens: int,
    model: str,
    thinking: str = "",
    thoughts_tokens: int = 0,
) -> Extraction:
    lines = []
    for item in payload.get("line_items") or []:
        lines.append(
            ExtractedLine(
                description=str(item.get("description") or "").strip(),
                sku=_blank(item.get("sku")),
                quantity=parse_decimal(item.get("quantity")),
                unit_price=parse_decimal(item.get("unit_price")),
                line_total=parse_decimal(item.get("line_total")),
            )
        )
    evidence = tuple(
        Evidence(field=str(item.get("field") or ""), quote=str(item.get("quote") or ""))
        for item in payload.get("evidence") or []
        if item.get("quote")
    )
    return Extraction(
        vendor_name=_blank(payload.get("vendor_name")),
        vendor_tax_id=_blank(payload.get("vendor_tax_id")),
        invoice_number=_blank(payload.get("invoice_number")),
        invoice_date=_blank(payload.get("invoice_date")),
        po_number=_blank(payload.get("po_number")),
        grn_number=_blank(payload.get("grn_number")),
        currency=_blank(payload.get("currency")),
        line_items=tuple(lines),
        subtotal=parse_decimal(payload.get("subtotal")),
        tax_rate_percent=parse_decimal(payload.get("tax_rate_percent")),
        tax_amount=parse_decimal(payload.get("tax_amount")),
        total=parse_decimal(payload.get("total")),
        proposed_action=_blank(payload.get("proposed_action")),
        model_note=_blank(payload.get("model_note")),
        evidence=evidence,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking=thinking,
        thoughts_tokens=thoughts_tokens,
    )


def _prompt(document_text: str, filename: str) -> str:
    schema = json.dumps(RESPONSE_SCHEMA)
    return (
        "Extract the vendor invoice in the document. "
        "Copy values exactly as printed. Use an empty string for missing text. "
        "Use -1 for a missing number. Do not correct arithmetic. "
        "tax_rate_percent is the printed percent, so 18% is 18. "
        "proposed_action is only a suggestion. "
        "Choose post_invoice only when the printed subtotal, tax, and total agree. "
        "evidence.quote must be a substring of the document. "
        "Reply with one JSON object matching this schema:\n"
        f"{schema}\n\n"
        f"Filename: {filename}\n\n"
        f"DOCUMENT:\n{document_text}"
    )


def _payload_from_response(response: object) -> tuple[dict | None, str]:
    """Gemma often returns JSON text without filling response.parsed."""
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed, "sdk_parsed"
    text = getattr(response, "text", None) or ""
    stripped = text.strip()
    if not stripped:
        return None, "empty"
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        return None, "json_error"
    if isinstance(loaded, dict):
        return loaded, "text_json"
    return None, "not_object"


def _empty_or_blocked(response: object) -> bool:
    payload, _source = _payload_from_response(response)
    if payload is not None:
        return False
    finish = ""
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        finish = str(getattr(candidates[0], "finish_reason", "") or "").upper()
    text = getattr(response, "text", None) or ""
    return (not text) or "RECITATION" in finish or "SAFETY" in finish


def _transient(exc: Exception) -> bool:
    text = str(exc).upper()
    return (
        "503" in text
        or "500" in text
        or "429" in text
        or "INTERNAL" in text
        or "UNAVAILABLE" in text
    )


def _label(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        return None
    value = match.group(1).strip()
    if value:
        return value
    remainder = text[match.end() :].lstrip("\r\n")
    if not remainder:
        return ""
    following = remainder.splitlines()[0].strip()
    if not following or following.endswith(":") or _NEXT_IS_LABEL.match(following):
        return ""
    return following


def _percent(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().rstrip("%").strip()


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _blank(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text == "-1":
        return None
    return text
