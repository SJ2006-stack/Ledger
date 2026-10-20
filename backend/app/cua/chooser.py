"""Ask Groq for one workbook action. Python still decides whether it may run."""

from __future__ import annotations

import json

from app.config import Settings
from app.cua.lessons import system_prompt
from app.cua.schemas import MAX_TEXT, ComputerAction, Observation, PolicyError
from app.llm import generate_json

ACTION_NAMES = (
    "open_workbook",
    "select_sheet",
    "search_sheet",
    "add_row",
    "edit_cell",
    "save_workbook",
    "dismiss_dialog",
)

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "action": {"type": "string", "enum": list(ACTION_NAMES)},
        "sheet": {"type": "string"},
        "search_value": {"type": "string"},
        "row": {"type": "integer"},
        "column": {"type": "string"},
        "value": {"type": "string"},
        "expected_result": {"type": "string"},
    },
    "required": ["goal", "action", "expected_result"],
}


def snapshot_text(obs: Observation) -> str:
    lines = [
        f"url: {obs.url or 'about:blank'}",
        f"sheet: {obs.sheet or 'none'}",
        f"dialog: {obs.dialog or 'none'}",
        f"filter: {obs.search_query or 'none'}",
        f"save: {obs.save_state}",
        "controls: sheet tabs, filter box, Apply filter, Add row, Save, formula bar",
    ]
    shown_rows = obs.rows[:8]
    for row in shown_rows:
        cells = [f"{key}={value}" for key, value in row.cells.items() if value]
        lines.append(f"row {row.index}: {', '.join(cells) if cells else '(blank)'}")
    if obs.row_count > len(shown_rows):
        lines.append(f"{obs.row_count} rows on this sheet")
    for tab in obs.tabs:
        lines.append(f"tab {tab.label}: {', '.join(tab.headers)}")
    return "\n".join(lines)


def gate_model_action(action: ComputerAction, intended: dict[str, str] | None) -> None:
    """Reject a model write the rules did not already allow."""
    if action.action == "edit_cell":
        if not intended:
            raise PolicyError("The rules do not allow a ledger edit.")
        column = action.column or ""
        wanted = intended.get(column, "")
        typed = action.value or ""
        if not _same(typed, wanted):
            raise PolicyError(f"The rules did not allow {column} to be {typed}.")
        return
    if action.action == "add_row" and not intended:
        raise PolicyError("The rules do not allow a new ledger row.")
    if action.action == "save_workbook" and not intended:
        raise PolicyError("The rules do not allow this invoice to be saved.")


class ActionChooser:
    """One model reply per step. Two rejected replies hand the rest of the task to choose()."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.failures = 0
        self.last_error = ""
        self.example: ComputerAction | None = None
        self.lesson_text = ""

    def use_lessons(self, text: str) -> None:
        self.lesson_text = text

    def teach(self, example: ComputerAction | None) -> None:
        """The next action from the ledger procedure. Groq is asked to send this action."""
        self.example = example

    @property
    def give_up(self) -> bool:
        return self.failures >= 2

    def note_failure(self, reason: str) -> None:
        self.last_error = reason
        self.failures += 1

    def propose(self, job: object, obs: Observation, mismatch: str) -> ComputerAction | None:
        if self.give_up or not self.settings.groq_api_key or self.settings.extractor == "template":
            return None
        try:
            return _generate(
                self.settings,
                _prompt(job, obs, mismatch, self.example),
                self.lesson_text or system_prompt(),
            )
        except Exception:
            self.note_failure("The model reply could not be used.")
            return None


def _procedure(example: ComputerAction | None) -> str:
    """The same order the deterministic ledger procedure uses. Groq sends the next step."""
    lines = [
        "Follow the ledger procedure. You send the next action. Python still rejects a value the rules do not allow.",
        "1. Open the ledger if it is closed.",
        "2. If a dialog is open, dismiss it before any other action.",
        "3. Open Vendors and filter to the vendor lookup.",
        "4. Open Purchase_Orders and filter to the PO. If that tab is missing, choose a live tab whose headers include PO ID, Ordered Qty, and Unit Price.",
        "5. Open Goods_Receipts and filter to the GRN. The headers must include GRN ID, PO ID, and Received Qty.",
        "6. Open Invoice_Ledger and filter to the invoice id. If that invoice is already stored with the allowed values, stop.",
        "7. Clear the filter. If no blank Invoice ID row is open, add_row once. If a blank row is already open, do not add_row again.",
        "8. Type the allowed columns on that blank row, one edit_cell at a time, in ledger order, using only the exact allowed value.",
        "9. Save once. Then filter to the invoice and read it back.",
        "Do not invent a total, tax, vendor, date, or status. Do not type APPROVED.",
    ]
    if example is None:
        lines.append("The procedure has no further action on this screen. Do not add a row, edit a cell, or save.")
        return "\n".join(lines)
    payload = {
        "goal": example.goal,
        "action": example.action,
        "sheet": example.sheet or "",
        "search_value": example.search_value or "",
        "row": example.row,
        "column": example.column or "",
        "value": example.value or "",
        "expected_result": example.expected_result,
    }
    lines.append("The procedure's next action for this screen is the example below. Reply with that same action.")
    lines.append(json.dumps(payload))
    return "\n".join(lines)


def _prompt(job: object, obs: Observation, mismatch: str, example: ComputerAction | None = None) -> str:
    intended = getattr(job, "intended", None)
    if isinstance(intended, dict) and intended:
        allowed = "\n".join(f"{key}: {value}" for key, value in intended.items())
    else:
        allowed = "none"
    mismatch_line = f"\nThe last action did not match the screen: {mismatch}\n" if mismatch else ""
    invoice_id = getattr(job, "invoice_id", "")
    vendor_query = getattr(job, "vendor_query", "") or "none"
    po_query = getattr(job, "po_query", "") or "none"
    grn_query = getattr(job, "grn_query", "") or "none"
    return (
        "You are looking at the ledger grid. Reply with one JSON action.\n"
        "If the sheet is not where you expected, you may include an actions array of navigation steps. Only the first action is run, then the sheet is read again.\n"
        f"Allowed actions: {', '.join(ACTION_NAMES)}.\n"
        "The usual tabs are Invoice_Ledger, Purchase_Orders, Goods_Receipts, and Vendors.\n"
        "If a usual tab is missing, choose a tab from the live list whose headers and rows match the goal. Do not invent a tab name.\n"
        "Type a cell only when an allowed row is listed, and only with that exact value.\n"
        "Do not invent a total, tax, or status. Do not use POSTED unless that exact status is in the allowed row.\n"
        f"{_procedure(example)}\n"
        f"Invoice {invoice_id}. Vendor lookup {vendor_query}. PO {po_query}. GRN {grn_query}.\n"
        f"Allowed row:\n{allowed}\n"
        f"{mismatch_line}"
        f"Screen:\n{snapshot_text(obs)}\n"
    )


def _generate(settings: Settings, prompt: str, system: str) -> ComputerAction:
    full_prompt = (
        f"{prompt}\nReply with one JSON object matching this schema:\n"
        f"{json.dumps(ACTION_SCHEMA)}"
    )
    reply = generate_json(
        settings.groq_api_key,
        settings.model,
        full_prompt,
        max_tokens=512,
        system=system,
    )
    parsed = _first_proposal(reply.payload)
    for key in ("goal", "expected_result"):
        if isinstance(parsed.get(key), str):
            parsed[key] = parsed[key][:MAX_TEXT]
    if parsed.get("value") is not None:
        parsed["value"] = str(parsed["value"])[:MAX_TEXT]
    parsed.setdefault("timeout_ms", 8_000)
    return ComputerAction.model_validate(parsed)


def _first_proposal(parsed: dict) -> dict:
    """A model may propose several navigation steps. Python runs only the first one."""
    actions = parsed.get("actions")
    if isinstance(actions, list) and actions and isinstance(actions[0], dict):
        return actions[0]
    return parsed


def _same(current: str, wanted: str) -> bool:
    if current == wanted:
        return True
    if not current and not wanted:
        return True
    try:
        return float(current) == float(wanted)
    except ValueError:
        return False
