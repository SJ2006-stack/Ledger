from app.cua.schemas import MAX_TIMEOUT_MS, ComputerAction, Observation

RETRY_BUDGET = 2

FAULT_CODES = (
    "sheet_not_found",
    "action_timeout",
    "bridge_unreachable",
    "navigation_failure",
    "write_rejected",
    "unknown_modal",
    "save_confirmation_missing",
)


class ActionFault(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def classify_fault(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code in FAULT_CODES:
        return str(code)
    name = type(exc).__name__.casefold()
    text = str(exc).casefold()
    if "timeout" in name or "timed out" in text or "timeout" in text:
        return "action_timeout"
    if "closed" in text or "not open" in text:
        return "navigation_failure"
    if "rejected" in text:
        return "write_rejected"
    if "not found" in text or "no tab" in text:
        return "sheet_not_found"
    return "bridge_unreachable"


def ledger_is_open(url: str) -> bool:
    return "/workbook" in url or "/ledger" in url or "docs.google.com/spreadsheets/" in url


def action_succeeded(action: ComputerAction, after: Observation) -> bool:
    kind = action.action
    if kind == "open_workbook":
        return ledger_is_open(after.url)
    if kind == "select_sheet":
        return after.sheet == action.sheet
    if kind == "search_sheet":
        return after.search_query == (action.search_value or "")
    if kind == "add_row":
        # Controllers also check row_count grew; never treat add_row as vacuously ok.
        return after.row_count > 0
    if kind == "edit_cell":
        column = action.column or ""
        for row in after.rows:
            if row.index == action.row:
                return row.cells.get(column, "") == (action.value or "")
        return False
    if kind == "save_workbook":
        return after.save_state in {"saved", "error", "unconfirmed"} or after.dialog in {
            "save_error",
            "lost_response",
        }
    if kind == "dismiss_dialog":
        return after.dialog == ""
    return False


def within_budget(attempts: int, budget: int = RETRY_BUDGET) -> bool:
    return attempts < budget


def timeout_allowed(timeout_ms: int) -> bool:
    return 1 <= timeout_ms <= MAX_TIMEOUT_MS


def compare_row(actual: dict[str, str], intended: dict[str, str]) -> list[str]:
    mismatches: list[str] = []
    for column, wanted in intended.items():
        if column == "Processed At":
            continue
        seen = actual.get(column, "")
        if seen == wanted:
            continue
        try:
            if float(seen) == float(wanted):
                continue
        except ValueError:
            pass
        mismatches.append(f"{column}: saw {seen!r}, wanted {wanted!r}")
    return mismatches


def row_matches(actual: dict[str, str], intended: dict[str, str]) -> bool:
    return len(compare_row(actual, intended)) == 0
