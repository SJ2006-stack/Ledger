"""Read and write the signed-in Google Sheet.

The local workbook page is not a destination. This client talks to an Apps
Script web app that runs as the signed-in account. Tests pass a fake transport.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.config import Settings
from app.cua.memory_browser import MemoryBrowser
from app.cua.recovery import ActionFault
from app.cua.schemas import COLUMNS, SHEET_NAMES, GridRow, Observation, SheetTab
from app.cua.workbook_io import seed_rows

SPREADSHEET_ID = "19DpNYaW7I_nleAjqT1XBmA7VnZzUQBfOWNRx0xwlrLk"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"

BRIDGE_SOURCE = r"""
const SPREADSHEET_ID = "19DpNYaW7I_nleAjqT1XBmA7VnZzUQBfOWNRx0xwlrLk";

function doPost(e) {
  const body = JSON.parse((e.postData && e.postData.contents) || "{}");
  const book = SpreadsheetApp.openById(SPREADSHEET_ID);
  if (body.action === "read") return json_({ok: true, sheets: read_(book)});
  if (body.action === "write") {
    write_(book, body.sheets || {});
    return json_({ok: true, sheets: read_(book)});
  }
  return json_({ok: false, error: "Unknown action."});
}

function read_(book) {
  const out = {};
  book.getSheets().forEach((sheet) => {
    const values = sheet.getDataRange().getDisplayValues();
    out[sheet.getName()] = values;
  });
  return out;
}

function write_(book, sheets) {
  Object.keys(sheets).forEach((name) => {
    let sheet = book.getSheetByName(name);
    if (!sheet) sheet = book.insertSheet(name);
    const rows = sheets[name] || [];
    sheet.clear();
    if (!rows.length || !rows[0].length) return;
    const width = rows[0].length;
    const rectangular = rows.map((row) => {
      const copy = row.slice(0, width).map((cell) => String(cell == null ? "" : cell));
      while (copy.length < width) copy.push("");
      return copy;
    });
    sheet.getRange(1, 1, rectangular.length, width).setValues(rectangular);
  });
  const leftover = book.getSheetByName("Sheet1");
  if (leftover && book.getSheets().length > 1) book.deleteSheet(leftover);
}

function json_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(ContentService.MimeType.JSON);
}
""".strip()


class GoogleSheetClient:
    def __init__(self, webapp_url: str, transport=None) -> None:
        self.webapp_url = webapp_url.strip()
        self.transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> "GoogleSheetClient":
        return cls(settings.google_sheet_webapp_url)

    def connected(self) -> bool:
        return bool(self.webapp_url) or self.transport is not None

    def read_book(self) -> dict[str, dict[str, list]]:
        payload = self._call("read")
        return _book(payload.get("sheets") or {})

    def read_lists(self) -> dict[str, list[list[str]]]:
        book = self.read_book()
        return {name: list(book[name]["rows"]) for name in SHEET_NAMES if name in book}

    def read_dicts(self) -> dict[str, list[dict[str, str]]]:
        return _dicts(self.read_lists())

    def write(
        self,
        sheets: dict[str, list[list[str]]],
        headers: dict[str, list[str]] | None = None,
    ) -> dict[str, list[list[str]]]:
        grids: dict[str, list[list[str]]] = {}
        names = list(headers) if headers is not None else list(SHEET_NAMES)
        for name in names:
            header = list(headers[name]) if headers is not None else list(COLUMNS[name])
            grids[name] = [header, *[list(row) for row in sheets.get(name, [])]]
        payload = self._call("write", {"sheets": grids})
        return _lists(payload.get("sheets") or {})

    def blank(self) -> bool:
        book = self.read_book()
        return not any(info["rows"] for info in book.values())

    def approve(self, invoice_id: str) -> dict:
        invoice_id = invoice_id.strip()
        if not invoice_id:
            raise RuntimeError("Name the invoice to approve.")
        book = self.read_book()
        ledger = book.get("Invoice_Ledger")
        if ledger is None:
            raise RuntimeError("The Google Sheet has no invoice ledger.")
        headers = list(ledger["headers"])
        if "Status" not in headers or "Invoice ID" not in headers:
            raise RuntimeError("The invoice ledger is missing Status or Invoice ID.")
        status_at = headers.index("Status")
        id_at = headers.index("Invoice ID")
        matched: list[list[str]] | None = None
        for row in ledger["rows"]:
            padded = list(row) + [""] * (len(headers) - len(row))
            if padded[id_at] == invoice_id:
                matched = padded
                break
        if matched is None:
            raise RuntimeError(f"{invoice_id} is not on the Google Sheet.")
        current = matched[status_at]
        if current == "APPROVED":
            return {"ok": True, "changed": False, "invoice_id": invoice_id, "status": "APPROVED", "row": _row_dict(headers, matched)}
        if current != "NEEDS_APPROVAL":
            raise RuntimeError(f"{invoice_id} is {current or 'blank'} and cannot be approved.")
        matched[status_at] = "APPROVED"
        sheets = {name: [list(item) for item in info["rows"]] for name, info in book.items()}
        sheets["Invoice_Ledger"] = [
            matched if (list(row) + [""] * (len(headers) - len(row)))[id_at] == invoice_id else list(row)
            for row in ledger["rows"]
        ]
        header_map = {name: list(info["headers"]) for name, info in book.items()}
        self.write(sheets, header_map)
        confirmed = self.read_book()["Invoice_Ledger"]
        seen = next(
            (
                _row_dict(confirmed["headers"], row)
                for row in confirmed["rows"]
                if (list(row) + [""])[confirmed["headers"].index("Invoice ID")] == invoice_id
            ),
            None,
        )
        if seen is None or seen.get("Status") != "APPROVED":
            raise RuntimeError("The Google Sheet approval could not be read back.")
        return {"ok": True, "changed": True, "invoice_id": invoice_id, "status": "APPROVED", "row": seen}

    def _call(self, action: str, extra: dict | None = None) -> dict:
        if not self.connected():
            raise RuntimeError("Google Sheets is not connected.")
        body = {"action": action}
        if extra:
            body.update(extra)
        if self.transport is not None:
            try:
                loaded = self.transport(body)
            except TimeoutError as exc:
                raise ActionFault("action_timeout", "The Google Sheet bridge timed out.") from exc
        else:
            loaded = _post_json(self.webapp_url, body)
        if not isinstance(loaded, dict) or not loaded.get("ok"):
            detail = loaded.get("error") if isinstance(loaded, dict) else "Google Sheets did not answer."
            raise RuntimeError(str(detail or "Google Sheets rejected the request."))
        # #region agent log
        try:
            sheets = loaded.get("sheets") or {}
            ledger = sheets.get("Invoice_Ledger") if isinstance(sheets, dict) else None
            with open("/Users/shrianshjaiswal/prototype/.cursor/debug-6a3e14.log", "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"sessionId": "6a3e14", "hypothesisId": "C", "location": "google_sheet.py:_call", "message": "bridge call", "data": {"action": action, "ok": True, "ledger_rows": len(ledger) - 1 if isinstance(ledger, list) and ledger else 0}, "timestamp": int(time.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion
        return loaded


def _chrome_sheet_state(*, reload: bool, close_extras: bool) -> str:
    """Focus the one signed-in sheet tab. Returns ready, loading, or missing."""
    script = (
        'tell application "Google Chrome"\n'
        "activate\n"
        "set keeper to missing value\n"
        "set keeperWindow to missing value\n"
        "set keeperIndex to 0\n"
        "repeat with w in windows\n"
        "set i to (count of tabs of w)\n"
        "repeat while i > 0\n"
        "set t to tab i of w\n"
        f'if URL of t contains "{SPREADSHEET_ID}" then\n'
        "if keeper is missing value then\n"
        "set keeper to t\n"
        "set keeperWindow to w\n"
        "set keeperIndex to i\n"
        f"else if {str(close_extras).lower()} then\n"
        "close t\n"
        "end if\n"
        "end if\n"
        "set i to i - 1\n"
        "end repeat\n"
        "end repeat\n"
        "if keeper is missing value then return \"missing\"\n"
        "set active tab index of keeperWindow to keeperIndex\n"
        "set index of keeperWindow to 1\n"
        f"if {str(reload).lower()} then reload keeper\n"
        "if loading of keeper then return \"loading\"\n"
        "return \"ready\"\n"
        "end tell"
    )
    result = subprocess.run(["osascript", "-e", script], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return "missing"
    return (result.stdout or "").strip() or "missing"


def _wait_for_signed_in_sheet(*, reload: bool) -> None:
    """Open at most one tab, then wait until that Google Sheet has loaded."""
    state = _chrome_sheet_state(reload=False, close_extras=True)
    opened = False
    if state == "missing":
        subprocess.run(["open", "-a", "Google Chrome", SHEET_URL], check=False)
        opened = True
    elif reload and state == "ready":
        _chrome_sheet_state(reload=True, close_extras=False)
        time.sleep(1.0)
    deadline = time.time() + 30
    while time.time() < deadline:
        state = _chrome_sheet_state(reload=False, close_extras=False)
        if state == "ready":
            # #region agent log
            try:
                with open("/Users/shrianshjaiswal/prototype/.cursor/debug-6a3e14.log", "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"sessionId": "6a3e14", "hypothesisId": "E", "location": "google_sheet.py:wait", "message": "sheet tab ready", "data": {"reload": reload, "opened": opened}, "timestamp": int(time.time() * 1000)}) + "\n")
            except Exception:
                pass
            # #endregion
            return
        if state == "missing" and not opened:
            subprocess.run(["open", "-a", "Google Chrome", SHEET_URL], check=False)
            opened = True
        time.sleep(0.4)
    # #region agent log
    try:
        with open("/Users/shrianshjaiswal/prototype/.cursor/debug-6a3e14.log", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"sessionId": "6a3e14", "hypothesisId": "E", "location": "google_sheet.py:wait", "message": "sheet tab wait ended", "data": {"state": state, "reload": reload, "opened": opened}, "timestamp": int(time.time() * 1000)}) + "\n")
    except Exception:
        pass
    # #endregion


class GoogleSheetBrowser(MemoryBrowser):
    """Same clicks as the ledger worker, persisted on the Google Sheet."""

    def __init__(self, client: GoogleSheetClient, *, visible: bool = False, persistent: bool = True) -> None:
        super().__init__(_UNUSED_PATH)
        self.client = client
        self.visible = visible
        self.persistent = persistent
        self.headers: dict[str, list[str]] = {}
        self.order: list[str] = []

    def open(self, fault: str | None = None, *, reset: bool = False) -> None:
        if reset or self.client.blank():
            self.client.write(seed_rows())
        book = self.client.read_book()
        self.order = list(book)
        self.headers = {name: list(info["headers"]) for name, info in book.items()}
        self.sheets = {name: [list(row) for row in info["rows"]] for name, info in book.items()}
        self.sheet = "Invoice_Ledger" if "Invoice_Ledger" in self.sheets else (self.order[0] if self.order else "Invoice_Ledger")
        self.search_query = ""
        self.dialog = ""
        self.save_state = "idle"
        self.fault = fault
        self.fault_used = False
        self.url = "about:blank"
        self.opened = True
        if self.visible:
            self._show(reload=False)

    def close(self, *, shutdown: bool = False) -> None:
        del shutdown
        self.opened = False

    def act(self, action) -> None:
        # #region agent log
        try:
            with open("/Users/shrianshjaiswal/prototype/.cursor/debug-6a3e14.log", "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"sessionId": "6a3e14", "hypothesisId": "B", "location": "google_sheet.py:act", "message": "sheet action", "data": {"action": action.action, "row": action.row, "column": action.column, "value": (action.value or "")[:40], "ledger_rows": len(self.sheets.get("Invoice_Ledger", []))}, "timestamp": int(time.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion
        if action.action == "select_sheet" and action.sheet not in self.sheets:
            raise ActionFault("sheet_not_found", f"No tab named {action.sheet}.")
        if action.action == "add_row":
            headers = self.headers.get("Invoice_Ledger") or list(COLUMNS["Invoice_Ledger"])
            self.sheets.setdefault("Invoice_Ledger", []).append([""] * len(headers))
            self.actions.append(action.action)
        elif action.action == "edit_cell":
            headers = self.headers.get("Invoice_Ledger") or list(COLUMNS["Invoice_Ledger"])
            column = action.column or ""
            if column not in headers:
                raise ActionFault("write_rejected", f"Unknown column {column}.")
            row_index = action.row or 0
            position = headers.index(column)
            grid = self.sheets.setdefault("Invoice_Ledger", [])
            while len(grid) <= row_index:
                grid.append([""] * len(headers))
            row = grid[row_index]
            if len(row) < len(headers):
                row.extend([""] * (len(headers) - len(row)))
            row[position] = action.value or ""
            self.actions.append(action.action)
        else:
            super().act(action)
        if action.action == "open_workbook":
            self.url = SHEET_URL
            self._show(reload=False)
        elif action.action == "save_workbook" and self.save_state == "saved":
            self._show(reload=True)

    def _show(self, *, reload: bool) -> None:
        if not self.visible:
            return
        _wait_for_signed_in_sheet(reload=reload)

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
        book = self.client.read_book()
        ledger = book.get("Invoice_Ledger") or {"headers": [], "rows": []}
        return [_row_dict(ledger["headers"], row) for row in ledger["rows"] if any(row)]

    def _save(self) -> None:
        # #region agent log
        try:
            with open("/Users/shrianshjaiswal/prototype/.cursor/debug-6a3e14.log", "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"sessionId": "6a3e14", "hypothesisId": "B", "location": "google_sheet.py:_save", "message": "save requested", "data": {"ledger_rows": len(self.sheets.get("Invoice_Ledger", [])), "tabs": list(self.sheets)}, "timestamp": int(time.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion
        if self.fault == "save_failure" and not self.fault_used:
            self.fault_used = True
            self.dialog = "save_error"
            self.save_state = "error"
            return
        self.client.write(self.sheets, self.headers)
        if self.fault == "lost_response" and not self.fault_used:
            self.fault_used = True
            self.dialog = "lost_response"
            self.save_state = "unconfirmed"
            return
        self.dialog = ""
        self.save_state = "saved"


_UNUSED_PATH = Path("/tmp/ledger-google-sheet-not-used.xlsx")


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "text/plain;charset=utf-8"},
        method="POST",
    )
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
            status = response.status
    except TimeoutError as exc:
        raise ActionFault("action_timeout", "The Google Sheet bridge timed out.") from exc
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode(errors="replace")
    html = raw.lstrip().startswith("<") or "<html" in raw[:240].casefold()
    # #region agent log
    try:
        with open("/Users/shrianshjaiswal/prototype/.cursor/debug-6a3e14.log", "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "sessionId": "6a3e14",
                        "hypothesisId": "bridge",
                        "location": "google_sheet.py:_post_json",
                        "message": "bridge response",
                        "data": {"status": status, "html": html, "json": raw.lstrip().startswith("{")},
                        "timestamp": int(time.time() * 1000),
                    }
                )
                + "\n"
            )
    except Exception:
        pass
    # #endregion
    if html or status >= 400:
        raise ActionFault("bridge_unreachable", _bridge_message(status))
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActionFault("bridge_unreachable", _bridge_message(status)) from exc
    return loaded if isinstance(loaded, dict) else {"ok": False, "error": "Google Sheets returned an unexpected payload."}


def _bridge_message(status: int) -> str:
    if status in {401, 403}:
        return "The Google Sheet web app refused the connection. Deploy it again, execute as yourself, and set access to Anyone."
    if status == 404:
        return "The Google Sheet web app URL was not found. Use the deployment address that ends in /exec."
    return "The Google Sheet web app did not return the ledger."


def _book(raw: dict) -> dict[str, dict[str, list]]:
    book: dict[str, dict[str, list]] = {}
    for name, grid in raw.items():
        if not isinstance(grid, list):
            continue
        rows = [["" if cell is None else str(cell) for cell in row] for row in grid if isinstance(row, list)]
        known = list(COLUMNS[name]) if name in COLUMNS else []
        if rows and known and [cell.strip() for cell in rows[0][: len(known)]] == known:
            book[name] = {"headers": known, "rows": rows[1:]}
            continue
        if rows:
            book[name] = {"headers": [cell.strip() for cell in rows[0]], "rows": rows[1:]}
            continue
        book[name] = {"headers": known, "rows": []}
    return book


def _row_dict(headers: list[str], row: list) -> dict[str, str]:
    padded = ["" if cell is None else str(cell) for cell in row]
    return {column: padded[index] if index < len(padded) else "" for index, column in enumerate(headers)}


def _lists(raw: dict) -> dict[str, list[list[str]]]:
    loaded: dict[str, list[list[str]]] = {}
    for name in SHEET_NAMES:
        grid = raw.get(name) or []
        rows = [["" if cell is None else str(cell) for cell in row] for row in grid if isinstance(row, list)]
        header = list(COLUMNS[name])
        if rows and [cell.strip() for cell in rows[0][: len(header)]] == header:
            rows = rows[1:]
        loaded[name] = rows
    return loaded


def _dicts(sheets: dict[str, list[list[str]]]) -> dict[str, list[dict[str, str]]]:
    parsed: dict[str, list[dict[str, str]]] = {}
    for name in SHEET_NAMES:
        columns = COLUMNS[name]
        parsed[name] = [
            {column: row[index] if index < len(row) else "" for index, column in enumerate(columns)}
            for row in sheets.get(name, [])
            if any(row)
        ]
    return parsed
