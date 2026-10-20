import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    tax_id TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY,
    po_number TEXT NOT NULL UNIQUE,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    tax_rate_bps INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS po_lines (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    sku TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS goods_receipts (
    id INTEGER PRIMARY KEY,
    grn_number TEXT NOT NULL UNIQUE,
    po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    vendor_id INTEGER NOT NULL REFERENCES vendors(id)
);

CREATE TABLE IF NOT EXISTS grn_lines (
    id INTEGER PRIMARY KEY,
    grn_id INTEGER NOT NULL REFERENCES goods_receipts(id),
    sku TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    vendor_id INTEGER REFERENCES vendors(id),
    vendor_name TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    po_number TEXT,
    grn_number TEXT,
    invoice_date TEXT,
    currency TEXT NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    tax_rate_bps INTEGER NOT NULL,
    tax_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    task_id TEXT,
    decision TEXT NOT NULL,
    explanation TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS invoices_vendor_number
    ON invoices(vendor_id, invoice_number)
    WHERE vendor_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    sku TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    line_total_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    instruction TEXT NOT NULL,
    filename TEXT,
    status TEXT NOT NULL,
    decision TEXT,
    explanation TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    invoice_id INTEGER,
    verified INTEGER NOT NULL DEFAULT 0,
    issues_json TEXT NOT NULL DEFAULT '[]',
    model_action TEXT,
    final_action TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    step TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_rows (
    id INTEGER PRIMARY KEY,
    invoice_id TEXT,
    vendor TEXT NOT NULL DEFAULT '',
    tax_id TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL DEFAULT '',
    po_id TEXT NOT NULL DEFAULT '',
    grn_id TEXT NOT NULL DEFAULT '',
    subtotal TEXT NOT NULL DEFAULT '',
    tax_rate TEXT NOT NULL DEFAULT '',
    tax_amount TEXT NOT NULL DEFAULT '',
    total TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    exception TEXT NOT NULL DEFAULT '',
    source_pdf TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS ledger_invoice_id
    ON ledger_rows(invoice_id)
    WHERE invoice_id IS NOT NULL AND invoice_id != '';
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
