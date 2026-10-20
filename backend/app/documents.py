from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

from app.config import ROOT

UPLOADS = ROOT / "backend" / "data" / "uploads"
GREEN = (22 / 255, 56 / 255, 44 / 255)
PAPER = (250 / 255, 247 / 255, 239 / 255)
INK = (28 / 255, 25 / 255, 21 / 255)
MUTED = (92 / 255, 86 / 255, 76 / 255)
LINE = (228 / 255, 220 / 255, 203 / 255)
WHITE = (1, 1, 1)


@dataclass(frozen=True)
class LineSpec:
    sku: str
    description: str
    quantity: str
    unit_price: str
    line_total: str


@dataclass(frozen=True)
class InvoiceSpec:
    filename: str
    vendor_name: str
    vendor_tax_id: str
    invoice_number: str
    invoice_date: str
    po_number: str
    grn_number: str
    currency: str
    lines: tuple[LineSpec, ...]
    subtotal: str
    tax_rate_percent: str
    tax_amount: str
    total: str


def render_text(spec: InvoiceSpec) -> str:
    rows = [
        spec.vendor_name,
        f"Tax ID: {spec.vendor_tax_id}",
        f"Invoice Number: {spec.invoice_number}",
        f"Invoice Date: {spec.invoice_date}",
        f"Purchase Order: {spec.po_number}",
        f"Goods Receipt: {spec.grn_number}",
        f"Currency: {spec.currency}",
        "Items:",
    ]
    for line in spec.lines:
        rows.append(
            "Item: "
            f"{line.sku} | {line.description} | qty {line.quantity} | "
            f"unit {line.unit_price} | amount {line.line_total}"
        )
    rows.extend(
        [
            f"Subtotal: {spec.subtotal}",
            f"Tax Rate: {spec.tax_rate_percent}%",
            f"Tax: {spec.tax_amount}",
            f"Total: {spec.total}",
        ]
    )
    return "\n".join(rows)


PACK = ROOT / "mini_hyperbots_invoice_pack"


def pack_file(filename: str) -> Path | None:
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return None
    path = (PACK / filename).resolve()
    if path.parent != PACK.resolve() or path.suffix.lower() != ".pdf" or not path.is_file():
        return None
    return path


def pack_filenames() -> list[str]:
    if not PACK.is_dir():
        return []
    return sorted(path.name for path in PACK.glob("*.pdf"))


def packed_pdf(filename: str) -> bytes | None:
    path = pack_file(filename)
    if path is None:
        return None
    return path.read_bytes()


def invoice_pdf(spec: InvoiceSpec) -> bytes:
    packed = packed_pdf(spec.filename)
    if packed is not None:
        return packed
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.draw_rect(page.rect, fill=PAPER, color=None)
    page.draw_rect(fitz.Rect(0, 0, 595, 132), fill=GREEN, color=None)
    page.insert_text((40, 64), spec.vendor_name, fontsize=22, fontname="hebo", color=WHITE)
    page.insert_text((40, 98), "Tax invoice", fontsize=12, fontname="helv", color=(0.78, 0.86, 0.80))
    page.insert_text((400, 58), spec.currency or "INR", fontsize=11, fontname="helv", color=WHITE)
    page.insert_text((400, 84), spec.invoice_number, fontsize=18, fontname="hebo", color=WHITE)
    page.insert_text((400, 108), spec.invoice_date, fontsize=11, fontname="helv", color=WHITE)

    rows = [row for row in render_text(spec).splitlines() if row != spec.vendor_name]
    meta = [row for row in rows if not row.startswith("Item:") and row != "Items:" and not _is_total(row)]
    totals = [row for row in rows if _is_total(row)]

    page.draw_rect(fitz.Rect(32, 156, 563, 156 + 36 + len(meta) * 22), fill=WHITE, color=LINE)
    y = 180
    for row in meta:
        page.insert_text((48, y), row, fontsize=12, fontname="helv", color=INK)
        y += 22

    table_top = y + 28
    table_bottom = table_top + 36 + max(len(spec.lines), 1) * 28
    page.draw_rect(fitz.Rect(32, table_top, 563, table_bottom), fill=WHITE, color=LINE)
    page.draw_rect(fitz.Rect(32, table_top, 563, table_top + 28), fill=GREEN, color=None)
    page.insert_text((48, table_top + 18), "SKU", fontsize=10, fontname="hebo", color=WHITE)
    page.insert_text((140, table_top + 18), "Description", fontsize=10, fontname="hebo", color=WHITE)
    page.insert_text((300, table_top + 18), "Qty", fontsize=10, fontname="hebo", color=WHITE)
    page.insert_text((360, table_top + 18), "Unit price", fontsize=10, fontname="hebo", color=WHITE)
    page.insert_text((470, table_top + 18), "Amount", fontsize=10, fontname="hebo", color=WHITE)
    item_y = table_top + 48
    for line in spec.lines:
        page.insert_text((48, item_y), line.sku, fontsize=11, fontname="helv", color=INK)
        page.insert_text((140, item_y), line.description, fontsize=11, fontname="helv", color=INK)
        page.insert_text((300, item_y), line.quantity, fontsize=11, fontname="helv", color=INK)
        page.insert_text((360, item_y), line.unit_price, fontsize=11, fontname="helv", color=INK)
        page.insert_text((470, item_y), line.line_total, fontsize=11, fontname="helv", color=INK)
        item_y += 28

    totals_top = table_bottom + 22
    page.draw_rect(fitz.Rect(300, totals_top, 563, totals_top + 16 + len(totals) * 22), fill=WHITE, color=LINE)
    total_y = totals_top + 24
    for row in totals:
        weight = "hebo" if row.startswith("Total:") else "helv"
        page.insert_text((320, total_y), row, fontsize=12, fontname=weight, color=INK)
        total_y += 22

    page.insert_text(
        (40, 800),
        "Synthetic invoice for the ledger prototype. Not a document for payment.",
        fontsize=9,
        fontname="helv",
        color=MUTED,
    )
    payload = document.tobytes()
    document.close()
    return payload


def _is_total(row: str) -> bool:
    return row.startswith(("Subtotal:", "Tax Rate:", "Tax:", "Total:"))


def pdf_preview(payload: bytes) -> bytes:
    document = fitz.open(stream=payload, filetype="pdf")
    try:
        if document.page_count == 0:
            raise ValueError("The PDF has no pages.")
        return document[0].get_pixmap(dpi=140).tobytes("png")
    finally:
        document.close()


def store_task_pdf(task_id: str, payload: bytes) -> None:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / f"{task_id}.pdf").write_bytes(payload)


def task_pdf_path(task_id: str) -> str:
    return str(UPLOADS / f"{task_id}.pdf")


def invoice_pdf_path(invoice_id: int) -> str:
    return str(UPLOADS / f"invoice-{invoice_id}.pdf")


def poor_scan_pdf() -> bytes:
    packed = packed_pdf("scan-unreadable.pdf")
    if packed is not None:
        return packed
    image = Image.new("RGB", (800, 1000), (248, 248, 246))
    draw = ImageDraw.Draw(image)
    draw.text((48, 80), "Invoice Number: INV-0000", fill=(246, 246, 244))
    draw.text((48, 120), "Total: 100.00", fill=(247, 247, 245))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buffer.getvalue())
    payload = document.tobytes()
    document.close()
    return payload


CLEAN = InvoiceSpec(
    filename="northwind-inv-2001.pdf",
    vendor_name="Northwind Supplies",
    vendor_tax_id="27AABCU9603R1ZM",
    invoice_number="INV-2001",
    invoice_date="2026-10-01",
    po_number="PO-1001",
    grn_number="GRN-1001",
    currency="INR",
    lines=(LineSpec("SKU-WA", "Widget A", "100", "50.00", "5000.00"),),
    subtotal="5000.00",
    tax_rate_percent="18",
    tax_amount="900.00",
    total="5900.00",
)

MISMATCH = InvoiceSpec(
    filename="harbor-inv-2002.pdf",
    vendor_name="Harbor Components",
    vendor_tax_id="29AABCH1234F1Z5",
    invoice_number="INV-2002",
    invoice_date="2026-10-02",
    po_number="PO-2002",
    grn_number="GRN-2002",
    currency="INR",
    lines=(LineSpec("SKU-BB", "Bolt B", "100", "10.00", "1000.00"),),
    subtotal="1000.00",
    tax_rate_percent="18",
    tax_amount="180.00",
    total="1180.00",
)

DUPLICATE = InvoiceSpec(
    filename="apex-inv-3003.pdf",
    vendor_name="Apex Industrial",
    vendor_tax_id="07AADCA1111B1Z8",
    invoice_number="INV-3003",
    invoice_date="2026-09-15",
    po_number="PO-3003",
    grn_number="GRN-3003",
    currency="INR",
    lines=(LineSpec("SKU-CX", "Cable X", "10", "200.00", "2000.00"),),
    subtotal="2000.00",
    tax_rate_percent="18",
    tax_amount="360.00",
    total="2360.00",
)

MISSING_PO = InvoiceSpec(
    filename="northwind-inv-2200.pdf",
    vendor_name="Northwind Supplies",
    vendor_tax_id="27AABCU9603R1ZM",
    invoice_number="INV-2200",
    invoice_date="2026-10-03",
    po_number="PO-9999",
    grn_number="",
    currency="INR",
    lines=(LineSpec("SKU-WA", "Widget A", "100", "50.00", "5000.00"),),
    subtotal="5000.00",
    tax_rate_percent="18",
    tax_amount="900.00",
    total="5900.00",
)

TAX_ERROR = InvoiceSpec(
    filename="northwind-inv-2100.pdf",
    vendor_name="Northwind Supplies",
    vendor_tax_id="27AABCU9603R1ZM",
    invoice_number="INV-2100",
    invoice_date="2026-10-04",
    po_number="PO-1001",
    grn_number="GRN-1001",
    currency="INR",
    lines=(LineSpec("SKU-WA", "Widget A", "100", "50.00", "5000.00"),),
    subtotal="5000.00",
    tax_rate_percent="18",
    tax_amount="100.00",
    total="5100.00",
)

AMBIGUOUS = InvoiceSpec(
    filename="helios-inv-5005.pdf",
    vendor_name="Helios",
    vendor_tax_id="",
    invoice_number="INV-5005",
    invoice_date="2026-10-05",
    po_number="PO-5005",
    grn_number="GRN-5005",
    currency="INR",
    lines=(LineSpec("SKU-HM", "Plate HM", "5", "1000.00", "5000.00"),),
    subtotal="5000.00",
    tax_rate_percent="18",
    tax_amount="900.00",
    total="5900.00",
)

UNKNOWN_VENDOR = InvoiceSpec(
    filename="cascade-inv-7001.pdf",
    vendor_name="Cascade Logistics",
    vendor_tax_id="",
    invoice_number="INV-7001",
    invoice_date="2026-10-06",
    po_number="",
    grn_number="",
    currency="INR",
    lines=(LineSpec("SKU-ZZ", "Crate", "2", "25.00", "50.00"),),
    subtotal="50.00",
    tax_rate_percent="0",
    tax_amount="0.00",
    total="50.00",
)

RETRY_CLEAN = InvoiceSpec(
    filename="northwind-inv-2010.pdf",
    vendor_name="Northwind Supplies",
    vendor_tax_id="27AABCU9603R1ZM",
    invoice_number="INV-2010",
    invoice_date="2026-10-07",
    po_number="PO-1001",
    grn_number="GRN-1001",
    currency="INR",
    lines=(LineSpec("SKU-WA", "Widget A", "100", "50.00", "5000.00"),),
    subtotal="5000.00",
    tax_rate_percent="18",
    tax_amount="900.00",
    total="5900.00",
)

LOST_RESPONSE = InvoiceSpec(
    filename="northwind-inv-2011.pdf",
    vendor_name="Northwind Supplies",
    vendor_tax_id="27AABCU9603R1ZM",
    invoice_number="INV-2011",
    invoice_date="2026-10-08",
    po_number="PO-1001",
    grn_number="GRN-1001",
    currency="INR",
    lines=(LineSpec("SKU-WA", "Widget A", "100", "50.00", "5000.00"),),
    subtotal="5000.00",
    tax_rate_percent="18",
    tax_amount="900.00",
    total="5900.00",
)

BAD_PRICE = InvoiceSpec(
    filename="northwind-inv-2012.pdf",
    vendor_name="Northwind Supplies",
    vendor_tax_id="27AABCU9603R1ZM",
    invoice_number="INV-2012",
    invoice_date="2026-10-09",
    po_number="PO-1001",
    grn_number="GRN-1001",
    currency="INR",
    lines=(LineSpec("SKU-WA", "Widget A", "100", "999.00", "99900.00"),),
    subtotal="99900.00",
    tax_rate_percent="18",
    tax_amount="17982.00",
    total="117882.00",
)

SAMPLES: dict[str, InvoiceSpec] = {
    spec.filename: spec
    for spec in (
        CLEAN,
        MISMATCH,
        DUPLICATE,
        MISSING_PO,
        TAX_ERROR,
        AMBIGUOUS,
        UNKNOWN_VENDOR,
    )
}

CATALOG: dict[str, InvoiceSpec] = {
    spec.filename: spec
    for spec in (
        *SAMPLES.values(),
        RETRY_CLEAN,
        LOST_RESPONSE,
        BAD_PRICE,
    )
}

POOR_SCAN_NAME = "scan-unreadable.pdf"
