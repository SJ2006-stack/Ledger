"""Ledger grids stored in SQL. The computer-use worker uses the same actions as before."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.cua.memory_browser import MemoryBrowser
from app.cua.recovery import ActionFault
from app.cua.schemas import COLUMNS, SHEET_NAMES, GridRow, Observation, SheetTab
from app.money import cents_to_str, milli_to_str

LEDGER_URL = "http://127.0.0.1/ledger"
_UNUSED_PATH = Path("/tmp/ledger-sql-not-used.xlsx")

_LEDGER_FIELDS = (
    ("Invoice ID", "invoice_id"),
    ("Vendor", "vendor"),
    ("Tax ID", "tax_id"),
    ("Invoice Date", "invoice_date"),
    ("PO ID", "po_id"),
    ("GRN ID", "grn_id"),
    ("Subtotal", "subtotal"),
    ("Tax Rate", "tax_rate"),
    ("Tax Amount", "tax_amount"),
    ("Total", "total"),
    ("Status", "status"),
    ("Exception", "exception"),
    ("Source PDF", "source_pdf"),
    ("Processed At", "processed_at"),
)


class LedgerBook:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.Lock()

    def grids(self) -> dict[str, list[list[str]]]:
        with self._lock:
            return {
                "Invoice_Ledger": self._ledger_rows(),
                "Purchase_Orders": self._po_rows(),
                "Goods_Receipts": self._grn_rows(),
                "Vendors": self._vendor_rows(),
            }

    def ledger_dicts(self) -> list[dict[str, str]]:
        with self._lock:
            return [_row_dict(row) for row in self._ledger_rows()]

    def replace_ledger(self, rows: list[list[str]]) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ledger_rows")
            for raw in rows:
                if not any(str(cell).strip() for cell in raw):
                    continue
                values = _padded(raw)
                invoice_id = values[0].strip() or None
                self.conn.execute(
                    """
                    INSERT INTO ledger_rows (
                        invoice_id, vendor, tax_id, invoice_date, po_id, grn_id,
                        subtotal, tax_rate, tax_amount, total, status, exception,
                        source_pdf, processed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (invoice_id, *values[1:]),
                )
            self.conn.commit()

    def approve(self, invoice_id: str) -> dict:
        invoice_id = invoice_id.strip()
        if not invoice_id:
            raise RuntimeError("Name the invoice to approve.")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM ledger_rows WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"{invoice_id} is not on the ledger.")
            current = row["status"]
            if current == "APPROVED":
                return {
                    "ok": True,
                    "changed": False,
                    "invoice_id": invoice_id,
                    "status": "APPROVED",
                    "row": _dict_from_sql(row),
                }
            if current != "NEEDS_APPROVAL":
                raise RuntimeError(f"{invoice_id} is {current or 'blank'} and cannot be approved.")
            self.conn.execute(
                "UPDATE ledger_rows SET status = ? WHERE invoice_id = ?",
                ("APPROVED", invoice_id),
            )
            self.conn.commit()
            confirmed = self.conn.execute(
                "SELECT * FROM ledger_rows WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchone()
        if confirmed is None or confirmed["status"] != "APPROVED":
            raise RuntimeError("The ledger approval could not be read back.")
        return {
            "ok": True,
            "changed": True,
            "invoice_id": invoice_id,
            "status": "APPROVED",
            "row": _dict_from_sql(confirmed),
        }

    def _ledger_rows(self) -> list[list[str]]:
        rows = self.conn.execute("SELECT * FROM ledger_rows ORDER BY id").fetchall()
        return [[row[field] or "" if field == "invoice_id" else row[field] for _label, field in _LEDGER_FIELDS] for row in rows]

    def _vendor_rows(self) -> list[list[str]]:
        rows = self.conn.execute("SELECT id, name, tax_id FROM vendors ORDER BY id").fetchall()
        return [[str(row["id"]), row["name"], row["tax_id"], "Yes", "Net 30"] for row in rows]

    def _po_rows(self) -> list[list[str]]:
        rows = self.conn.execute(
            """
            SELECT purchase_orders.po_number, vendors.name AS vendor, po_lines.sku,
                   po_lines.description, po_lines.quantity_milli, po_lines.unit_price_cents,
                   purchase_orders.currency
            FROM po_lines
            JOIN purchase_orders ON purchase_orders.id = po_lines.po_id
            JOIN vendors ON vendors.id = purchase_orders.vendor_id
            ORDER BY po_lines.id
            """
        ).fetchall()
        return [
            [
                row["po_number"],
                row["vendor"],
                row["sku"],
                row["description"],
                milli_to_str(row["quantity_milli"]),
                cents_to_str(row["unit_price_cents"]),
                row["currency"],
            ]
            for row in rows
        ]

    def _grn_rows(self) -> list[list[str]]:
        rows = self.conn.execute(
            """
            SELECT goods_receipts.grn_number, purchase_orders.po_number, grn_lines.sku,
                   grn_lines.quantity_milli
            FROM grn_lines
            JOIN goods_receipts ON goods_receipts.id = grn_lines.grn_id
            JOIN purchase_orders ON purchase_orders.id = goods_receipts.po_id
            ORDER BY grn_lines.id
            """
        ).fetchall()
        return [
            [row["grn_number"], row["po_number"], row["sku"], milli_to_str(row["quantity_milli"]), ""]
            for row in rows
        ]


class SqlLedgerBrowser(MemoryBrowser):
    """Same clicks as the ledger worker, persisted in SQL."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__(_UNUSED_PATH)
        self.book = LedgerBook(conn)
        self.headers = {name: list(COLUMNS[name]) for name in SHEET_NAMES}
        self.order = list(SHEET_NAMES)
        self.saves = 0

    def open(self, fault: str | None = None, *, reset: bool = False) -> None:
        del reset
        self.sheets = {name: [list(row) for row in rows] for name, rows in self.book.grids().items()}
        self.sheet = "Invoice_Ledger"
        self.search_query = ""
        self.dialog = ""
        self.save_state = "idle"
        self.fault = fault
        self.fault_used = False
        self.url = "about:blank"
        self.opened = True
        self.actions = []

    def close(self, *, shutdown: bool = False) -> None:
        del shutdown
        self.opened = False

    def act(self, action) -> None:
        if action.action == "select_sheet" and action.sheet not in self.sheets:
            raise ActionFault("sheet_not_found", f"No tab named {action.sheet}.")
        if action.action == "add_row":
            headers = self.headers["Invoice_Ledger"]
            grid = self.sheets.setdefault("Invoice_Ledger", [])
            if any(not (row[0] if row else "").strip() for row in grid):
                raise ActionFault("write_rejected", "A blank ledger row is already open.")
            grid.append([""] * len(headers))
            self.actions.append(action.action)
        elif action.action == "edit_cell":
            headers = self.headers["Invoice_Ledger"]
            column = action.column or ""
            if column not in headers:
                raise ActionFault("write_rejected", f"Unknown column {column}.")
            row_index = action.row or 0
            grid = self.sheets.setdefault("Invoice_Ledger", [])
            while len(grid) <= row_index:
                grid.append([""] * len(headers))
            row = grid[row_index]
            if len(row) < len(headers):
                row.extend([""] * (len(headers) - len(row)))
            row[headers.index(column)] = action.value or ""
            self.actions.append(action.action)
        else:
            super().act(action)
        if action.action == "open_workbook":
            self.url = LEDGER_URL

    def observe(self, *, capture: bool = True) -> Observation:
        del capture
        headers = list(self.headers.get(self.sheet, []))
        raw_rows = self.sheets.get(self.sheet, [])
        rows: list[GridRow] = []
        for index, raw in enumerate(raw_rows):
            cells = {headers[pos]: raw[pos] if pos < len(raw) else "" for pos in range(len(headers))}
            if self.search_query and self.search_query.casefold() not in " ".join(cells.values()).casefold():
                continue
            rows.append(GridRow(index=index, cells=cells))
        tabs: list[SheetTab] = []
        for label in self.order:
            head = list(self.headers.get(label, []))
            preview: list[dict[str, str]] = []
            for raw in self.sheets.get(label, [])[:8]:
                preview.append({head[pos]: raw[pos] if pos < len(raw) else "" for pos in range(len(head))})
            tabs.append(SheetTab(label=label, headers=head, rows=preview))
        return Observation(
            url=self.url,
            sheet=self.sheet,
            dialog=self.dialog,
            save_state=self.save_state,
            search_query=self.search_query,
            columns=headers,
            rows=rows,
            row_count=len(raw_rows),
            tabs=tabs,
        )

    def read_back(self) -> list[dict[str, str]]:
        return self.book.ledger_dicts()

    def _save(self) -> None:
        if self.fault == "save_failure" and not self.fault_used:
            self.fault_used = True
            self.dialog = "save_error"
            self.save_state = "error"
            return
        self.book.replace_ledger(self.sheets.get("Invoice_Ledger", []))
        self.saves += 1
        if self.fault == "lost_response" and not self.fault_used:
            self.fault_used = True
            self.dialog = "lost_response"
            self.save_state = "unconfirmed"
            return
        self.dialog = ""
        self.save_state = "saved"


def sheets_payload(conn: sqlite3.Connection) -> dict:
    book = LedgerBook(conn)
    grids = book.grids()
    sheets = {}
    for name in SHEET_NAMES:
        columns = list(COLUMNS[name])
        sheets[name] = {
            "columns": columns,
            "rows": [_row_dict(row, columns) for row in grids[name]],
        }
    return {"sheets": sheets}


def _padded(raw: list[str]) -> list[str]:
    values = ["" if cell is None else str(cell) for cell in raw]
    width = len(_LEDGER_FIELDS)
    if len(values) < width:
        values.extend([""] * (width - len(values)))
    return values[:width]


def _row_dict(raw: list[str], columns: list[str] | None = None) -> dict[str, str]:
    headers = columns or [label for label, _field in _LEDGER_FIELDS]
    values = _padded(raw) if columns is None else list(raw)
    return {headers[pos]: values[pos] if pos < len(values) else "" for pos in range(len(headers))}


def _dict_from_sql(row: sqlite3.Row) -> dict[str, str]:
    return {label: "" if field == "invoice_id" and row[field] is None else str(row[field] or "") for label, field in _LEDGER_FIELDS}
