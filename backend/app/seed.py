import json
import sqlite3
from datetime import datetime, timezone

from app.money import decimal_to_cents, decimal_to_milli, parse_decimal, percent_to_bps


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT COUNT(*) AS n FROM vendors").fetchone()
    if existing["n"]:
        seed_ledger(conn)
        return

    vendors = [
        (1, "Northwind Supplies", "27AABCU9603R1ZM", []),
        (2, "Harbor Components", "29AABCH1234F1Z5", []),
        (3, "Apex Industrial", "07AADCA1111B1Z8", []),
        (4, "Helios Metals", "24AABCH5555A1Z1", []),
        (5, "Helios Metal Works", "24AABCH6666A1Z2", []),
    ]
    conn.executemany(
        "INSERT INTO vendors (id, name, tax_id, aliases_json) VALUES (?, ?, ?, ?)",
        [(vid, name, tax, json.dumps(aliases)) for vid, name, tax, aliases in vendors],
    )

    orders = [
        (1, "PO-1001", 1, 1800, "INR", "open"),
        (2, "PO-2002", 2, 1800, "INR", "open"),
        (3, "PO-3003", 3, 1800, "INR", "open"),
        (4, "PO-5005", 4, 1800, "INR", "open"),
        (5, "PO-5006", 5, 1800, "INR", "open"),
    ]
    conn.executemany(
        """
        INSERT INTO purchase_orders
            (id, po_number, vendor_id, tax_rate_bps, currency, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        orders,
    )
    po_lines = [
        (1, 1, "SKU-WA", "Widget A", "100", "50.00"),
        (2, 2, "SKU-BB", "Bolt B", "100", "10.00"),
        (3, 3, "SKU-CX", "Cable X", "10", "200.00"),
        (4, 4, "SKU-HM", "Plate HM", "5", "1000.00"),
        (5, 5, "SKU-HW", "Plate HW", "5", "1100.00"),
    ]
    conn.executemany(
        """
        INSERT INTO po_lines
            (id, po_id, sku, description, quantity_milli, unit_price_cents)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                lid,
                po_id,
                sku,
                description,
                decimal_to_milli(parse_decimal(qty) or parse_decimal("0")),
                decimal_to_cents(parse_decimal(price) or parse_decimal("0")),
            )
            for lid, po_id, sku, description, qty, price in po_lines
        ],
    )

    receipts = [
        (1, "GRN-1001", 1, 1),
        (2, "GRN-2002", 2, 2),
        (3, "GRN-3003", 3, 3),
        (4, "GRN-5005", 4, 4),
        (5, "GRN-5006", 5, 5),
    ]
    conn.executemany(
        """
        INSERT INTO goods_receipts (id, grn_number, po_id, vendor_id)
        VALUES (?, ?, ?, ?)
        """,
        receipts,
    )
    grn_lines = [
        (1, 1, "SKU-WA", "Widget A", "100"),
        (2, 2, "SKU-BB", "Bolt B", "90"),
        (3, 3, "SKU-CX", "Cable X", "10"),
        (4, 4, "SKU-HM", "Plate HM", "5"),
        (5, 5, "SKU-HW", "Plate HW", "5"),
    ]
    conn.executemany(
        """
        INSERT INTO grn_lines (id, grn_id, sku, description, quantity_milli)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                lid,
                grn_id,
                sku,
                description,
                decimal_to_milli(parse_decimal(qty) or parse_decimal("0")),
            )
            for lid, grn_id, sku, description, qty in grn_lines
        ],
    )

    subtotal = decimal_to_cents(parse_decimal("2000.00") or parse_decimal("0"))
    tax = decimal_to_cents(parse_decimal("360.00") or parse_decimal("0"))
    total = decimal_to_cents(parse_decimal("2360.00") or parse_decimal("0"))
    conn.execute(
        """
        INSERT INTO invoices (
            id, vendor_id, vendor_name, invoice_number, po_number, grn_number,
            invoice_date, currency, subtotal_cents, tax_rate_bps, tax_cents,
            total_cents, status, idempotency_key, task_id, decision, explanation,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            3,
            "Apex Industrial",
            "INV-3003",
            "PO-3003",
            "GRN-3003",
            "2026-09-15",
            "INR",
            subtotal,
            percent_to_bps(parse_decimal("18") or parse_decimal("0")),
            tax,
            total,
            "pending_approval",
            "seed-inv-3003",
            None,
            "ready",
            "Seeded posted invoice for duplicate detection.",
            _now(),
        ),
    )
    conn.execute(
        """
        INSERT INTO invoice_lines (
            invoice_id, sku, description, quantity_milli, unit_price_cents, line_total_cents
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "SKU-CX",
            "Cable X",
            decimal_to_milli(parse_decimal("10") or parse_decimal("0")),
            decimal_to_cents(parse_decimal("200.00") or parse_decimal("0")),
            subtotal,
        ),
    )
    conn.commit()
    seed_ledger(conn)


def seed_ledger(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT 1 FROM ledger_rows WHERE invoice_id = ?",
        ("INV-3003",),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """
        INSERT INTO ledger_rows (
            invoice_id, vendor, tax_id, invoice_date, po_id, grn_id,
            subtotal, tax_rate, tax_amount, total, status, exception,
            source_pdf, processed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "INV-3003",
            "Apex Industrial",
            "07AADCA1111B1Z8",
            "2026-09-15",
            "PO-3003",
            "GRN-3003",
            "2000.00",
            "18",
            "360.00",
            "2360.00",
            "NEEDS_APPROVAL",
            "",
            "apex-inv-3003.pdf",
            "seed",
        ),
    )
    conn.commit()
