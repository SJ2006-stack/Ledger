from typing import Literal

from pydantic import BaseModel, Field

SHEET_NAMES = ("Invoice_Ledger", "Purchase_Orders", "Goods_Receipts", "Vendors")

COLUMNS: dict[str, tuple[str, ...]] = {
    "Invoice_Ledger": (
        "Invoice ID",
        "Vendor",
        "Tax ID",
        "Invoice Date",
        "PO ID",
        "GRN ID",
        "Subtotal",
        "Tax Rate",
        "Tax Amount",
        "Total",
        "Status",
        "Exception",
        "Source PDF",
        "Processed At",
    ),
    "Purchase_Orders": (
        "PO ID",
        "Vendor",
        "SKU",
        "Description",
        "Ordered Qty",
        "Unit Price",
        "Currency",
    ),
    "Goods_Receipts": (
        "GRN ID",
        "PO ID",
        "SKU",
        "Received Qty",
        "Receipt Date",
    ),
    "Vendors": (
        "Vendor ID",
        "Legal Name",
        "Tax ID",
        "Active",
        "Payment Terms",
    ),
}

LEDGER_STATUSES = ("POSTED", "NEEDS_REVIEW", "NEEDS_APPROVAL", "PROCESSING_ERROR")

ActionName = Literal[
    "open_workbook",
    "select_sheet",
    "search_sheet",
    "add_row",
    "edit_cell",
    "save_workbook",
    "dismiss_dialog",
]

MAX_TIMEOUT_MS = 10_000
MAX_TEXT = 200


class PolicyError(ValueError):
    pass


class ComputerAction(BaseModel):
    goal: str
    action: ActionName
    sheet: str | None = None
    search_value: str | None = None
    row: int | None = None
    column: str | None = None
    value: str | None = None
    expected_result: str
    timeout_ms: int = 8_000


class GridRow(BaseModel):
    index: int
    cells: dict[str, str] = Field(default_factory=dict)


class SheetTab(BaseModel):
    label: str
    headers: list[str] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)


class Observation(BaseModel):
    url: str
    sheet: str = ""
    dialog: str = ""
    save_state: str = "idle"
    search_query: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[GridRow] = Field(default_factory=list)
    row_count: int = 0
    tabs: list[SheetTab] = Field(default_factory=list)
    screenshot: str | None = None

    def matching(self, column: str, query: str) -> list[GridRow]:
        needle = query.casefold()
        if not needle:
            return []
        return [row for row in self.rows if needle in row.cells.get(column, "").casefold()]


def validate_action(action: ComputerAction, *, origin: str, allowed_sheets: set[str] | None = None) -> None:
    if action.timeout_ms < 1 or action.timeout_ms > MAX_TIMEOUT_MS:
        raise PolicyError(f"Timeout {action.timeout_ms} ms is outside 1..{MAX_TIMEOUT_MS}.")
    if len(action.goal) > MAX_TEXT or len(action.expected_result) > MAX_TEXT:
        raise PolicyError("The goal or expected result is too long.")
    kind = action.action
    if kind == "open_workbook":
        if not origin.startswith("http://127.0.0.1:") and not origin.startswith("http://localhost:"):
            raise PolicyError("The workbook origin is not local.")
        return
    if kind == "select_sheet":
        allowed = set(SHEET_NAMES)
        if allowed_sheets:
            allowed.update(allowed_sheets)
        if not action.sheet or action.sheet not in allowed:
            raise PolicyError("That sheet is not part of the finance workbook.")
        return
    if kind == "search_sheet":
        if not action.search_value or len(action.search_value) > 80:
            raise PolicyError("The search value is missing or too long.")
        return
    if kind == "add_row":
        if action.sheet != "Invoice_Ledger":
            raise PolicyError("Rows are only added on the invoice ledger.")
        return
    if kind == "edit_cell":
        if action.sheet != "Invoice_Ledger":
            raise PolicyError("The agent may only type on the invoice ledger.")
        if action.row is None or action.row < 0 or action.row > 500:
            raise PolicyError("The target row is not valid.")
        columns = COLUMNS["Invoice_Ledger"]
        if action.column not in columns:
            raise PolicyError("That column is not on the invoice ledger.")
        if action.value is None or len(action.value) > MAX_TEXT:
            raise PolicyError("The cell value is missing or too long.")
        if action.column == "Status" and action.value not in LEDGER_STATUSES:
            raise PolicyError("The status is not one of the allowed ledger statuses.")
        return
    if kind == "save_workbook":
        return
    if kind == "dismiss_dialog":
        return
    raise PolicyError(f"Unknown action {kind}.")
