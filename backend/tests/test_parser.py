from decimal import Decimal

from app.documents import CLEAN, invoice_pdf, poor_scan_pdf, render_text
from app.extractor import TemplateExtractor
from app.parser import parse_pdf


def test_text_invoice_is_read_without_ocr():
    parsed = parse_pdf(invoice_pdf(CLEAN))
    assert parsed.quality == "text"
    assert parsed.used_ocr is False
    assert "INV-2001" in parsed.text
    assert "Northwind Supplies" in parsed.text
    extraction = TemplateExtractor().extract(parsed.text, CLEAN.filename)
    assert extraction.invoice_number == CLEAN.invoice_number
    assert extraction.po_number == CLEAN.po_number
    assert extraction.line_items[0].quantity == 100
    assert extraction.total == Decimal("5900.00")
    assert render_text(CLEAN).splitlines()[0] == "Northwind Supplies"


def test_faint_scan_is_poor_quality():
    parsed = parse_pdf(poor_scan_pdf())
    assert parsed.quality == "poor"
    assert parsed.used_ocr is True
