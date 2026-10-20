"""Worked ledger traces for the Groq system prompt.

The two seed traces come from the deterministic procedure. Each verified
invoice adds one more trace. The prompt keeps the seeds plus the latest
few new invoices.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SEED_PATH = Path(__file__).with_name("lesson_seed.json")
LEARNED_LIMIT = 4

RULES = (
    "You choose one ledger action at a time.\n"
    "Follow the worked examples. They are the ledger procedure's own steps.\n"
    "Use the allowed row for this invoice. Do not copy an example amount onto a different invoice.\n"
    "Open the ledger, check Vendors, then the PO tab, then the GRN tab, then the invoice ledger.\n"
    "If a usual tab is missing, choose a live tab whose headers match the lookup.\n"
    "If this invoice is already stored with the allowed values, stop. Do not add another row.\n"
    "If no blank Invoice ID row is open, add one row. If a blank row is already open, edit that row. Do not add a second blank row.\n"
    "Type each allowed column with that exact value, then save once and read the invoice back.\n"
    "Do not invent a total, tax, vendor, date, or status. Do not type APPROVED.\n"
    "Python rejects an action the rules do not allow."
)


def steps_from_audit(audit: list[dict]) -> list[dict]:
    steps: list[dict] = []
    for item in audit:
        if item.get("step") != "cua_act" or item.get("outcome") != "ok":
            continue
        detail = item.get("detail") or {}
        action = str(detail.get("action") or "")
        if not action:
            continue
        steps.append(
            {
                "goal": str(detail.get("goal") or ""),
                "action": action,
                "sheet": str(detail.get("sheet") or ""),
                "search": str(detail.get("search") or ""),
                "row": detail.get("row"),
                "column": str(detail.get("column") or ""),
                "value": str(detail.get("value") or ""),
            }
        )
    return steps


def load_seed() -> list[dict]:
    if not SEED_PATH.exists():
        return []
    loaded = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, list) else []


def seed_ids() -> set[str]:
    return {str(item.get("invoice_id") or "") for item in load_seed()}


def load_learned(conn: sqlite3.Connection) -> list[dict]:
    _ensure(conn)
    rows = conn.execute(
        "SELECT invoice_id, outcome, steps_json FROM cua_lessons ORDER BY updated_at ASC"
    ).fetchall()
    learned = []
    for row in rows:
        learned.append(
            {
                "invoice_id": row["invoice_id"],
                "outcome": row["outcome"],
                "steps": json.loads(row["steps_json"]),
            }
        )
    return learned


def remember_lesson(conn: sqlite3.Connection, invoice_id: str, outcome: str, audit: list[dict]) -> None:
    steps = steps_from_audit(audit)
    if not invoice_id or not steps:
        return
    _ensure(conn)
    conn.execute(
        """
        INSERT INTO cua_lessons (invoice_id, outcome, steps_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(invoice_id) DO UPDATE SET
            outcome = excluded.outcome,
            steps_json = excluded.steps_json,
            updated_at = excluded.updated_at
        """,
        (invoice_id, outcome, json.dumps(steps), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def system_prompt(learned: list[dict] | None = None) -> str:
    """Seeds stay. Newer invoices replace the oldest learned trace in the window."""
    parts = [RULES, "Worked examples from the ledger procedure:"]
    seeds = load_seed()
    for lesson in seeds:
        parts.append(format_lesson(lesson))
    fresh = [
        item
        for item in (learned or [])
        if str(item.get("invoice_id") or "") and str(item.get("invoice_id")) not in seed_ids()
    ]
    window = fresh[-LEARNED_LIMIT:]
    if window:
        parts.append("Invoices the procedure finished after those examples:")
        for lesson in window:
            parts.append(format_lesson(lesson))
    return "\n\n".join(parts)


def format_lesson(lesson: dict) -> str:
    lines = [
        f"Invoice {lesson.get('invoice_id') or 'unknown'} finished as {lesson.get('outcome') or 'recorded'}."
    ]
    for step in lesson.get("steps") or []:
        bits = [str(step.get("action") or "")]
        if step.get("sheet"):
            bits.append(f"sheet={step['sheet']}")
        if step.get("search"):
            bits.append(f"search={step['search']}")
        if step.get("row") is not None and step.get("action") == "edit_cell":
            bits.append(f"row={step['row']}")
        if step.get("column"):
            bits.append(f"column={step['column']}")
        if step.get("value"):
            bits.append(f"value={step['value']}")
        goal = step.get("goal") or ""
        lines.append(f"- {goal}: {', '.join(bits)}" if goal else f"- {', '.join(bits)}")
    return "\n".join(lines)


def signature(steps: list[dict]) -> list[tuple]:
    """Stable comparison. The processed-at stamp changes on every run."""
    signed = []
    for step in steps:
        value = step.get("value") or ""
        if step.get("column") == "Processed At":
            value = "<timestamp>"
        signed.append(
            (
                step.get("action") or "",
                step.get("sheet") or "",
                step.get("search") or "",
                step.get("column") or "",
                value,
                step.get("goal") or "",
            )
        )
    return signed


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cua_lessons (
            invoice_id TEXT PRIMARY KEY,
            outcome TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
