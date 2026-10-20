import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from app.money import (
    bps_to_str,
    cents_to_str,
    decimal_to_cents,
    decimal_to_milli,
    milli_to_str,
    parse_decimal,
    percent_to_bps,
)
from app.schemas import Decision


class DuplicateInvoice(Exception):
    def __init__(self, existing_id: int) -> None:
        self.existing_id = existing_id
        super().__init__(f"Invoice {existing_id} already exists for this vendor and number.")


class SaveFailed(Exception):
    pass


class InvalidTransition(Exception):
    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"Cannot change an invoice in status {status}.")


@dataclass(frozen=True)
class LineDraft:
    sku: str
    description: str
    quantity_milli: int
    unit_price_cents: int
    line_total_cents: int


@dataclass(frozen=True)
class InvoiceDraft:
    vendor_id: int | None
    vendor_name: str
    invoice_number: str
    po_number: str | None
    grn_number: str | None
    invoice_date: str | None
    currency: str
    subtotal_cents: int
    tax_rate_bps: int
    tax_cents: int
    total_cents: int
    status: str
    idempotency_key: str
    task_id: str | None
    decision: str
    explanation: str
    lines: tuple[LineDraft, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountingStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.fail_next = 0
        self._lock = threading.Lock()

    def create_invoice(self, draft: InvoiceDraft) -> tuple[dict, bool]:
        if draft.status not in {"pending_approval", "needs_review"}:
            raise InvalidTransition(draft.status)
        with self._lock:
            existing = self._invoice_by_key(draft.idempotency_key)
            if existing is not None:
                return existing, False
            if self.fail_next > 0:
                self.fail_next -= 1
                raise SaveFailed("The accounting database rejected the save.")
            if draft.vendor_id is not None:
                duplicate = self._invoice_by_vendor_number(draft.vendor_id, draft.invoice_number)
                if duplicate is not None:
                    raise DuplicateInvoice(int(duplicate["id"]))
            try:
                cursor = self.conn.execute(
                    """
                    INSERT INTO invoices (
                        vendor_id, vendor_name, invoice_number, po_number, grn_number,
                        invoice_date, currency, subtotal_cents, tax_rate_bps, tax_cents,
                        total_cents, status, idempotency_key, task_id, decision,
                        explanation, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.vendor_id,
                        draft.vendor_name,
                        draft.invoice_number,
                        draft.po_number,
                        draft.grn_number,
                        draft.invoice_date,
                        draft.currency,
                        draft.subtotal_cents,
                        draft.tax_rate_bps,
                        draft.tax_cents,
                        draft.total_cents,
                        draft.status,
                        draft.idempotency_key,
                        draft.task_id,
                        draft.decision,
                        draft.explanation,
                        _now(),
                    ),
                )
                invoice_id = int(cursor.lastrowid)
                self.conn.executemany(
                    """
                    INSERT INTO invoice_lines (
                        invoice_id, sku, description, quantity_milli,
                        unit_price_cents, line_total_cents
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            invoice_id,
                            line.sku,
                            line.description,
                            line.quantity_milli,
                            line.unit_price_cents,
                            line.line_total_cents,
                        )
                        for line in draft.lines
                    ],
                )
                self.conn.commit()
            except sqlite3.IntegrityError:
                self.conn.rollback()
                existing = self._invoice_by_key(draft.idempotency_key)
                if existing is not None:
                    return existing, False
                if draft.vendor_id is not None:
                    duplicate = self._invoice_by_vendor_number(draft.vendor_id, draft.invoice_number)
                    if duplicate is not None:
                        raise DuplicateInvoice(int(duplicate["id"])) from None
                raise
            saved = self._invoice_by_id(invoice_id)
            if saved is None:
                raise SaveFailed("The save committed but the row could not be read back.")
            return saved, True

    def get_invoice(self, invoice_id: int) -> dict | None:
        with self._lock:
            return self._invoice_by_id(invoice_id)

    def get_by_idempotency(self, key: str) -> dict | None:
        with self._lock:
            return self._invoice_by_key(key)

    def list_invoices(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM invoices ORDER BY id"
            ).fetchall()
            return [self._hydrate(row) for row in rows]

    def list_vendors(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM vendors ORDER BY id").fetchall()
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "tax_id": row["tax_id"],
                    "aliases": json.loads(row["aliases_json"]),
                }
                for row in rows
            ]

    def list_purchase_orders(self) -> list[dict]:
        with self._lock:
            orders = self.conn.execute(
                "SELECT * FROM purchase_orders ORDER BY id"
            ).fetchall()
            result = []
            for order in orders:
                lines = self.conn.execute(
                    "SELECT * FROM po_lines WHERE po_id = ? ORDER BY id",
                    (order["id"],),
                ).fetchall()
                result.append(
                    {
                        "id": order["id"],
                        "po_number": order["po_number"],
                        "vendor_id": order["vendor_id"],
                        "tax_rate": bps_to_str(order["tax_rate_bps"]),
                        "currency": order["currency"],
                        "status": order["status"],
                        "lines": [
                            {
                                "sku": line["sku"],
                                "description": line["description"],
                                "quantity": milli_to_str(line["quantity_milli"]),
                                "unit_price": cents_to_str(line["unit_price_cents"]),
                            }
                            for line in lines
                        ],
                    }
                )
            return result

    def list_goods_receipts(self) -> list[dict]:
        with self._lock:
            receipts = self.conn.execute(
                "SELECT * FROM goods_receipts ORDER BY id"
            ).fetchall()
            result = []
            for receipt in receipts:
                lines = self.conn.execute(
                    "SELECT * FROM grn_lines WHERE grn_id = ? ORDER BY id",
                    (receipt["id"],),
                ).fetchall()
                po = self.conn.execute(
                    "SELECT po_number FROM purchase_orders WHERE id = ?",
                    (receipt["po_id"],),
                ).fetchone()
                result.append(
                    {
                        "id": receipt["id"],
                        "grn_number": receipt["grn_number"],
                        "po_number": po["po_number"] if po else None,
                        "vendor_id": receipt["vendor_id"],
                        "lines": [
                            {
                                "sku": line["sku"],
                                "description": line["description"],
                                "quantity": milli_to_str(line["quantity_milli"]),
                            }
                            for line in lines
                        ],
                    }
                )
            return result

    def purchase_order(self, po_number: str) -> dict | None:
        with self._lock:
            order = self.conn.execute(
                "SELECT * FROM purchase_orders WHERE po_number = ?",
                (po_number,),
            ).fetchone()
            if order is None:
                return None
            lines = self.conn.execute(
                "SELECT * FROM po_lines WHERE po_id = ? ORDER BY id",
                (order["id"],),
            ).fetchall()
            return {
                "id": order["id"],
                "po_number": order["po_number"],
                "vendor_id": order["vendor_id"],
                "tax_rate_bps": order["tax_rate_bps"],
                "currency": order["currency"],
                "lines": [dict(line) for line in lines],
            }

    def goods_receipt(self, grn_number: str) -> dict | None:
        with self._lock:
            receipt = self.conn.execute(
                "SELECT * FROM goods_receipts WHERE grn_number = ?",
                (grn_number,),
            ).fetchone()
            if receipt is None:
                return None
            lines = self.conn.execute(
                "SELECT * FROM grn_lines WHERE grn_id = ? ORDER BY id",
                (receipt["id"],),
            ).fetchall()
            return {
                "id": receipt["id"],
                "grn_number": receipt["grn_number"],
                "po_id": receipt["po_id"],
                "vendor_id": receipt["vendor_id"],
                "lines": [dict(line) for line in lines],
            }

    def find_invoice(self, vendor_id: int, invoice_number: str) -> dict | None:
        with self._lock:
            return self._invoice_by_vendor_number(vendor_id, invoice_number)

    def approve(self, invoice_id: int) -> dict:
        return self._transition(invoice_id, "approved", {"pending_approval"})

    def reject(self, invoice_id: int) -> dict:
        return self._transition(invoice_id, "rejected", {"pending_approval", "needs_review"})

    def count_invoices(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()
            return int(row["n"])

    def count_with_number(self, invoice_number: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM invoices WHERE invoice_number = ?",
                (invoice_number,),
            ).fetchone()
            return int(row["n"])

    def tamper_total(self, invoice_id: int, total_cents: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE invoices SET total_cents = ? WHERE id = ?",
                (total_cents, invoice_id),
            )
            self.conn.commit()

    def insert_task(self, task: dict) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, instruction, filename, status, decision, explanation,
                    idempotency_key, invoice_id, verified, issues_json,
                    model_action, final_action, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task["id"],
                    task["instruction"],
                    task["filename"],
                    task["status"],
                    task["decision"],
                    task["explanation"],
                    task["idempotency_key"],
                    task["invoice_id"],
                    1 if task["verified"] else 0,
                    json.dumps(task["issues"]),
                    task["model_action"],
                    task["final_action"],
                    task["created_at"],
                    task["updated_at"],
                ),
            )
            self.conn.commit()

    def update_task(self, task: dict) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE tasks SET
                    status = ?, decision = ?, explanation = ?, invoice_id = ?,
                    verified = ?, issues_json = ?, model_action = ?,
                    final_action = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    task["status"],
                    task["decision"],
                    task["explanation"],
                    task["invoice_id"],
                    1 if task["verified"] else 0,
                    json.dumps(task["issues"]),
                    task["model_action"],
                    task["final_action"],
                    task["updated_at"],
                    task["id"],
                ),
            )
            self.conn.commit()

    def task_by_key(self, key: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            return self._task(row) if row else None

    def task_by_id(self, task_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return self._task(row) if row else None

    def list_tasks(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY created_at"
            ).fetchall()
            return [self._task(row) for row in rows]

    def add_audit(self, task_id: str, step: str, outcome: str, detail: dict) -> dict:
        created_at = _now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO audit_events (task_id, step, outcome, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, step, outcome, json.dumps(detail), created_at),
            )
            self.conn.commit()
        return {"step": step, "outcome": outcome, "detail": detail, "created_at": created_at}

    def audit_for(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT step, outcome, detail_json, created_at
                FROM audit_events WHERE task_id = ? ORDER BY id
                """,
                (task_id,),
            ).fetchall()
            return [
                {
                    "step": row["step"],
                    "outcome": row["outcome"],
                    "detail": json.loads(row["detail_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def _transition(self, invoice_id: int, status: str, allowed: set[str]) -> dict:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM invoices WHERE id = ?",
                (invoice_id,),
            ).fetchone()
            if row is None:
                raise KeyError(invoice_id)
            if row["status"] not in allowed:
                raise InvalidTransition(row["status"])
            self.conn.execute(
                "UPDATE invoices SET status = ? WHERE id = ?",
                (status, invoice_id),
            )
            self.conn.commit()
            saved = self._invoice_by_id(invoice_id)
            if saved is None:
                raise KeyError(invoice_id)
            return saved

    def _invoice_by_id(self, invoice_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if row is None:
            return None
        return self._hydrate(row)

    def _invoice_by_key(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM invoices WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return self._hydrate(row)

    def _invoice_by_vendor_number(self, vendor_id: int, invoice_number: str) -> dict | None:
        row = self.conn.execute(
            """
            SELECT * FROM invoices
            WHERE vendor_id = ? AND invoice_number = ?
            """,
            (vendor_id, invoice_number),
        ).fetchone()
        if row is None:
            return None
        return self._hydrate(row)

    def _hydrate(self, row: sqlite3.Row) -> dict:
        lines = self.conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "vendor_id": row["vendor_id"],
            "vendor_name": row["vendor_name"],
            "invoice_number": row["invoice_number"],
            "po_number": row["po_number"],
            "grn_number": row["grn_number"],
            "invoice_date": row["invoice_date"],
            "currency": row["currency"],
            "subtotal": cents_to_str(row["subtotal_cents"]),
            "tax_rate": bps_to_str(row["tax_rate_bps"]),
            "tax_amount": cents_to_str(row["tax_cents"]),
            "total": cents_to_str(row["total_cents"]),
            "subtotal_cents": row["subtotal_cents"],
            "tax_rate_bps": row["tax_rate_bps"],
            "tax_cents": row["tax_cents"],
            "total_cents": row["total_cents"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "task_id": row["task_id"],
            "decision": row["decision"],
            "explanation": row["explanation"],
            "created_at": row["created_at"],
            "lines": [
                {
                    "sku": line["sku"],
                    "description": line["description"],
                    "quantity": milli_to_str(line["quantity_milli"]),
                    "unit_price": cents_to_str(line["unit_price_cents"]),
                    "line_total": cents_to_str(line["line_total_cents"]),
                }
                for line in lines
            ],
        }

    def _task(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "instruction": row["instruction"],
            "filename": row["filename"],
            "status": row["status"],
            "decision": row["decision"],
            "explanation": row["explanation"],
            "idempotency_key": row["idempotency_key"],
            "invoice_id": row["invoice_id"],
            "verified": bool(row["verified"]),
            "issues": json.loads(row["issues_json"]),
            "model_action": row["model_action"],
            "final_action": row["final_action"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def draft_from_payload(payload: dict) -> InvoiceDraft:
    status = str(payload["status"])
    if status not in {"pending_approval", "needs_review"}:
        raise InvalidTransition(status)
    decision = payload.get("decision", Decision.READY.value)
    lines = []
    for line in payload["lines"]:
        quantity = parse_decimal(line["quantity"])
        price = parse_decimal(line["unit_price"])
        if quantity is None or price is None:
            raise ValueError("Invoice lines need quantity and unit price.")
        total = parse_decimal(line.get("line_total"))
        line_total = decimal_to_cents(total if total is not None else quantity * price)
        lines.append(
            LineDraft(
                sku=str(line.get("sku") or ""),
                description=str(line["description"]),
                quantity_milli=decimal_to_milli(quantity),
                unit_price_cents=decimal_to_cents(price),
                line_total_cents=line_total,
            )
        )
    subtotal = parse_decimal(payload["subtotal"])
    tax = parse_decimal(payload["tax_amount"])
    total = parse_decimal(payload["total"])
    rate = parse_decimal(payload["tax_rate_percent"])
    if None in {subtotal, tax, total, rate}:
        raise ValueError("Subtotal, tax, total, and tax rate are required.")
    assert subtotal is not None and tax is not None and total is not None and rate is not None
    return InvoiceDraft(
        vendor_id=payload.get("vendor_id"),
        vendor_name=str(payload["vendor_name"]),
        invoice_number=str(payload["invoice_number"]),
        po_number=payload.get("po_number") or None,
        grn_number=payload.get("grn_number") or None,
        invoice_date=payload.get("invoice_date") or None,
        currency=str(payload.get("currency") or "INR"),
        subtotal_cents=decimal_to_cents(subtotal),
        tax_rate_bps=percent_to_bps(rate),
        tax_cents=decimal_to_cents(tax),
        total_cents=decimal_to_cents(total),
        status=status,
        idempotency_key=str(payload["idempotency_key"]),
        task_id=payload.get("task_id"),
        decision=str(decision),
        explanation=str(payload.get("explanation") or ""),
        lines=tuple(lines),
    )
