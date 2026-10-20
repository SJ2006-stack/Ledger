from decimal import Decimal

import pytest

from app.accounting import (
    DuplicateInvoice,
    InvalidTransition,
    InvoiceDraft,
    LineDraft,
    SaveFailed,
    draft_from_payload,
)
from app.money import decimal_to_cents, decimal_to_milli, parse_decimal


def _draft(key: str, number: str = "INV-9001", vendor_id: int = 1) -> InvoiceDraft:
    subtotal = decimal_to_cents(parse_decimal("5000.00") or Decimal("0"))
    tax = decimal_to_cents(parse_decimal("900.00") or Decimal("0"))
    total = decimal_to_cents(parse_decimal("5900.00") or Decimal("0"))
    return InvoiceDraft(
        vendor_id=vendor_id,
        vendor_name="Northwind Supplies",
        invoice_number=number,
        po_number="PO-1001",
        grn_number="GRN-1001",
        invoice_date="2026-10-01",
        currency="INR",
        subtotal_cents=subtotal,
        tax_rate_bps=1800,
        tax_cents=tax,
        total_cents=total,
        status="pending_approval",
        idempotency_key=key,
        task_id=None,
        decision="ready",
        explanation="api",
        lines=(
            LineDraft(
                sku="SKU-WA",
                description="Widget A",
                quantity_milli=decimal_to_milli(parse_decimal("100") or Decimal("0")),
                unit_price_cents=decimal_to_cents(parse_decimal("50.00") or Decimal("0")),
                line_total_cents=subtotal,
            ),
        ),
    )


def test_seed_has_one_clean_and_one_short_receipt(store):
    vendors = {row["name"] for row in store.list_vendors()}
    assert "Northwind Supplies" in vendors
    assert "Helios Metals" in vendors
    receipts = {row["grn_number"]: row for row in store.list_goods_receipts()}
    assert receipts["GRN-1001"]["lines"][0]["quantity"] == "100"
    assert receipts["GRN-2002"]["lines"][0]["quantity"] == "90"
    assert store.count_with_number("INV-3003") == 1


def test_idempotent_create_returns_the_same_row(store):
    first, created = store.create_invoice(_draft("key-1"))
    second, again = store.create_invoice(_draft("key-1"))
    assert created is True
    assert again is False
    assert first["id"] == second["id"]
    assert store.count_with_number("INV-9001") == 1


def test_duplicate_vendor_and_number_is_rejected(store):
    store.create_invoice(_draft("key-1"))
    with pytest.raises(DuplicateInvoice):
        store.create_invoice(_draft("key-2"))
    assert store.count_with_number("INV-9001") == 1


def test_failed_save_writes_nothing(store):
    before = store.count_invoices()
    store.fail_next = 1
    with pytest.raises(SaveFailed):
        store.create_invoice(_draft("key-fail"))
    assert store.count_invoices() == before
    saved, created = store.create_invoice(_draft("key-fail"))
    assert created is True
    assert saved["invoice_number"] == "INV-9001"


def test_lost_response_replay_finds_the_existing_row(store):
    saved, created = store.create_invoice(_draft("key-lost"))
    assert created is True
    discarded = saved["id"]
    del discarded
    replay, created_again = store.create_invoice(_draft("key-lost"))
    assert created_again is False
    assert replay["id"] == saved["id"]
    assert store.count_with_number("INV-9001") == 1


def test_agent_cannot_insert_an_approved_invoice(store):
    draft = _draft("key-approved")
    approved = InvoiceDraft(
        vendor_id=draft.vendor_id,
        vendor_name=draft.vendor_name,
        invoice_number=draft.invoice_number,
        po_number=draft.po_number,
        grn_number=draft.grn_number,
        invoice_date=draft.invoice_date,
        currency=draft.currency,
        subtotal_cents=draft.subtotal_cents,
        tax_rate_bps=draft.tax_rate_bps,
        tax_cents=draft.tax_cents,
        total_cents=draft.total_cents,
        status="approved",
        idempotency_key=draft.idempotency_key,
        task_id=None,
        decision=draft.decision,
        explanation=draft.explanation,
        lines=draft.lines,
    )
    with pytest.raises(InvalidTransition):
        store.create_invoice(approved)


def test_approval_is_only_from_pending(store):
    saved, _created = store.create_invoice(_draft("key-approve"))
    approved = store.approve(saved["id"])
    assert approved["status"] == "approved"
    held, _created = store.create_invoice(_draft("key-held", number="INV-9002"))
    store.conn.execute(
        "UPDATE invoices SET status = 'needs_review' WHERE id = ?",
        (held["id"],),
    )
    store.conn.commit()
    with pytest.raises(InvalidTransition):
        store.approve(held["id"])
    rejected = store.reject(held["id"])
    assert rejected["status"] == "rejected"


def test_payload_parser_accepts_decimal_strings():
    draft = draft_from_payload(
        {
            "vendor_id": 1,
            "vendor_name": "Northwind Supplies",
            "invoice_number": "INV-API",
            "po_number": "PO-1001",
            "currency": "INR",
            "subtotal": "10.00",
            "tax_rate_percent": "18",
            "tax_amount": "1.80",
            "total": "11.80",
            "status": "pending_approval",
            "idempotency_key": "payload",
            "lines": [
                {
                    "sku": "SKU-WA",
                    "description": "Widget A",
                    "quantity": "2",
                    "unit_price": "5.00",
                }
            ],
        }
    )
    assert draft.total_cents == 1180
    assert draft.lines[0].quantity_milli == 2000
