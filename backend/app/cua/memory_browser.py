"""In-memory workbook that speaks the same actions as the page.

Unit tests use this. It is not the demo write path. A save still flushes an
xlsx so verification reads the file rather than this object's opinion.
"""

from __future__ import annotations

from pathlib import Path

from app.cua.schemas import COLUMNS, ComputerAction, GridRow, Observation
from app.cua.workbook_io import read_sheets, reset_workbook, write_sheets


class MemoryBrowser:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sheets: dict[str, list[list[str]]] = {}
        self.sheet = "Invoice_Ledger"
        self.search_query = ""
        self.dialog = ""
        self.save_state = "idle"
        self.url = ""
        self.fault: str | None = None
        self.fault_used = False
        self.opened = False
        self.actions: list[str] = []

    def open(self, fault: str | None = None, *, reset: bool = False) -> None:
        if reset or not self.path.exists():
            reset_workbook(self.path)
        self.sheets = _rows_to_lists(read_sheets(self.path))
        self.sheet = "Invoice_Ledger"
        self.search_query = ""
        self.dialog = ""
        self.save_state = "idle"
        self.fault = fault
        self.fault_used = False
        self.url = "about:blank"
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def observe(self, *, capture: bool = True) -> Observation:
        del capture
        columns = list(COLUMNS[self.sheet])
        rows: list[GridRow] = []
        for index, raw in enumerate(self.sheets.get(self.sheet, [])):
            cells = {column: raw[pos] if pos < len(raw) else "" for pos, column in enumerate(columns)}
            if self.search_query and self.search_query.casefold() not in " ".join(cells.values()).casefold():
                continue
            rows.append(GridRow(index=index, cells=cells))
        return Observation(
            url=self.url,
            sheet=self.sheet,
            dialog=self.dialog,
            save_state=self.save_state,
            search_query=self.search_query,
            columns=columns,
            rows=rows,
            row_count=len(self.sheets.get(self.sheet, [])),
        )

    def act(self, action: ComputerAction) -> None:
        self.actions.append(action.action)
        kind = action.action
        if kind == "open_workbook":
            fault = f"?fault={self.fault}" if self.fault else ""
            self.url = f"http://127.0.0.1/workbook{fault}"
            return
        if kind == "select_sheet":
            self.sheet = action.sheet or self.sheet
            self.search_query = ""
            return
        if kind == "search_sheet":
            self.search_query = action.search_value or ""
            return
        if kind == "add_row":
            width = len(COLUMNS["Invoice_Ledger"])
            self.sheets["Invoice_Ledger"].append([""] * width)
            return
        if kind == "edit_cell":
            column = action.column or ""
            row_index = action.row or 0
            position = COLUMNS["Invoice_Ledger"].index(column)
            grid = self.sheets["Invoice_Ledger"]
            while len(grid) <= row_index:
                grid.append([""] * len(COLUMNS["Invoice_Ledger"]))
            row = grid[row_index]
            if len(row) < len(COLUMNS["Invoice_Ledger"]):
                row.extend([""] * (len(COLUMNS["Invoice_Ledger"]) - len(row)))
            row[position] = action.value or ""
            return
        if kind == "save_workbook":
            self._save()
            return
        if kind == "dismiss_dialog":
            self.dialog = ""
            return
        raise RuntimeError(kind)

    def plant_dialog(self, name: str) -> None:
        self.dialog = name

    def _save(self) -> None:
        if self.fault == "save_failure" and not self.fault_used:
            self.fault_used = True
            self.dialog = "save_error"
            self.save_state = "error"
            return
        write_sheets(self.path, self.sheets)
        if self.fault == "lost_response" and not self.fault_used:
            self.fault_used = True
            self.dialog = "lost_response"
            self.save_state = "unconfirmed"
            return
        self.dialog = ""
        self.save_state = "saved"


def _rows_to_lists(loaded: dict[str, list[dict[str, str]]]) -> dict[str, list[list[str]]]:
    return {
        name: [[row.get(column, "") for column in COLUMNS[name]] for row in loaded[name]]
        for name in COLUMNS
    }
