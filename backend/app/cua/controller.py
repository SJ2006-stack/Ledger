"""Observe the workbook, choose one allowed action, then check the result.

The next action comes from the latest observation. A click is not treated as
success until the grid shows the expected sheet, cell, dialog, or save state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.cua.chooser import gate_model_action
from app.cua.recovery import (
    RETRY_BUDGET,
    action_succeeded,
    classify_fault,
    ledger_is_open,
    row_matches,
    within_budget,
)
from app.cua.schemas import COLUMNS, ComputerAction, Observation, PolicyError, validate_action

LEDGER = COLUMNS["Invoice_Ledger"]


@dataclass
class Job:
    invoice_id: str
    source_pdf: str
    vendor_query: str
    vendor_expect: str
    po_query: str
    po_expect: str
    grn_query: str
    grn_expect: str
    intended: dict[str, str] | None
    duplicate_fixture: bool = False
    needs_clarification: bool = False
    candidates: list[str] = field(default_factory=list)


@dataclass
class StepRecord:
    goal: str
    action: str
    sheet: str
    expected_result: str
    observed: str
    ok: bool
    recovery: str
    screenshot: str | None


@dataclass
class Progress:
    phase: str = "open"
    judged: dict[str, bool] = field(default_factory=dict)
    write_index: int | None = None
    column_index: int = 0
    save_attempts: int = 0
    unexpected_attempts: int = 0
    action_attempts: int = 0
    outcome: str = "RUNNING"
    reason: str = ""
    records: list[StepRecord] = field(default_factory=list)
    rejected_tabs: list[str] = field(default_factory=list)
    evidence: dict[str, dict[str, str]] = field(default_factory=dict)
    adaptations: list[str] = field(default_factory=list)
    adaptation_reported: bool = False
    handed_off: bool = False
    control: str = "deterministic"
    blocked_on: str | None = None
    pending_question: str | None = None


def run_controller(
    browser,
    job: Job,
    *,
    origin: str,
    budget: int = RETRY_BUDGET,
    on_step: Callable[[dict], None] | None = None,
    chooser=None,
) -> Progress:
    progress = Progress()
    if chooser is not None:
        progress.control = "model"
    mismatch = ""
    for _ in range(80):
        before = browser.observe(capture=False)
        if chooser is not None and not _given_up(chooser):
            _absorb(job, progress, before)
            if _finish_if_done(job, progress, before):
                break
        elif _finish_if_done(job, progress, before):
            break
        def _reject(proposed: ComputerAction, reason: str) -> None:
            if on_step is None:
                return
            on_step(
                _step_payload(
                    "rejected",
                    proposed,
                    before,
                    False,
                    "",
                    narrate=False,
                    plan=_plan_snapshot(job, progress),
                    gate_reason=reason,
                )
            )

        action = _pick(job, progress, before, chooser, mismatch, origin, on_reject=_reject)
        _note_handoff(progress, chooser, on_step, job)
        mismatch = ""
        if action is None:
            break
        try:
            labels = {tab.label for tab in before.tabs}
            validate_action(action, origin=origin, allowed_sheets=labels)
        except PolicyError as exc:
            progress.outcome = "FAILED"
            progress.reason = str(exc)
            break
        if on_step is not None:
            on_step(_step_payload("about_to", action, before, False, "", narrate=True, plan=_plan_snapshot(job, progress)))
        try:
            browser.act(action)
        except Exception as exc:
            code = classify_fault(exc)
            recovery = f"{code}: {exc}"
            try:
                after = browser.observe()
            except Exception:
                after = before
            if code == "sheet_not_found" and action.sheet:
                progress.rejected_tabs.append(action.sheet)
            elif code in {"action_timeout", "bridge_unreachable", "navigation_failure", "write_rejected"}:
                model_driving = chooser is not None and not _given_up(chooser)
                if model_driving:
                    chooser.note_failure(recovery)
                    if _given_up(chooser):
                        progress.action_attempts = 0
                else:
                    progress.action_attempts += 1
                    if not within_budget(progress.action_attempts, budget):
                        progress.outcome = "FAILED"
                        progress.reason = recovery
                        progress.records.append(_record(action, after, False, recovery))
                        if on_step is not None:
                            on_step(_step_payload("done", action, after, False, recovery, narrate=False, plan=_plan_snapshot(job, progress)))
                        break
            else:
                progress.outcome = "FAILED"
                progress.reason = recovery
                progress.records.append(_record(action, after, False, recovery))
                if on_step is not None:
                    on_step(_step_payload("done", action, after, False, recovery, narrate=False, plan=_plan_snapshot(job, progress)))
                break
            progress.records.append(_record(action, after, False, recovery))
            if on_step is not None:
                on_step(_step_payload("done", action, after, False, recovery, narrate=False, plan=_plan_snapshot(job, progress)))
            continue
        after = browser.observe()
        ok = action_succeeded(action, after)
        if action.action == "add_row":
            ok = after.row_count > before.row_count
        if action.action == "save_workbook" and after.save_state == "idle" and not after.dialog:
            ok = False
            progress.reason = "save_confirmation_missing: The save did not confirm."
        duration_ms = int(getattr(browser, "last_act_ms", 0) or 0)
        recovery = ""
        model_driving = chooser is not None and not _given_up(chooser)
        if after.dialog == "save_error":
            recovery = "The save did not persist. I will try again if the budget allows."
        elif after.dialog == "lost_response":
            recovery = "The confirmation was lost. I will look the invoice up instead of typing another row."
        elif action.action == "save_workbook" and not ok:
            recovery = "save_confirmation_missing: The save did not confirm. I will look the invoice up."
            progress.phase = "readback"
            progress.judged["readback"] = False
        elif not ok and model_driving and not after.dialog:
            recovery = "The screen did not show the expected result, so the same goal stays open."
            chooser.note_failure(recovery)
            mismatch = recovery
            if _given_up(chooser):
                progress.action_attempts = 0
        elif not ok:
            progress.action_attempts += 1
            recovery = recovery or "The screen did not show the expected result, so the same goal stays open."
            if not within_budget(progress.action_attempts, budget):
                progress.outcome = "UNVERIFIED"
                progress.reason = recovery
                progress.records.append(_record(action, after, False, recovery))
                if on_step is not None:
                    on_step(_step_payload("done", action, after, False, recovery, narrate=False, plan=_plan_snapshot(job, progress)))
                break
        else:
            progress.action_attempts = 0
        _remember_visible(job, progress, after)
        progress.records.append(_record(action, after, ok, recovery))
        if on_step is not None:
            on_step(
                _step_payload(
                    "done",
                    action,
                    after,
                    ok,
                    recovery,
                    narrate=False,
                    duration_ms=duration_ms,
                    attempt=progress.action_attempts,
                    plan=_plan_snapshot(job, progress),
                    adapted=_adaptation_payload(progress),
                )
            )
    else:
        if progress.outcome == "RUNNING":
            progress.outcome = "FAILED"
            progress.reason = "The action budget was used up."
    if progress.outcome == "RUNNING":
        progress.outcome = "READY_TO_VERIFY"
    return progress


def _pick(
    job: Job,
    progress: Progress,
    obs: Observation,
    chooser,
    mismatch: str,
    origin: str,
    on_reject: Callable[[ComputerAction, str], None] | None = None,
) -> ComputerAction | None:
    if chooser is None or _given_up(chooser) or obs.dialog:
        return choose(job, progress, obs)
    labels = {tab.label for tab in obs.tabs}
    proposed = chooser.propose(job, obs, mismatch)
    if proposed is None:
        return choose(job, progress, obs)
    for _ in range(2):
        try:
            validate_action(proposed, origin=origin, allowed_sheets=labels)
            gate_model_action(proposed, job.intended)
            return proposed
        except PolicyError as exc:
            chooser.note_failure(str(exc))
            if on_reject is not None:
                on_reject(proposed, str(exc))
            if _given_up(chooser):
                return choose(job, progress, obs)
            proposed = chooser.propose(job, obs, str(exc))
            if proposed is None:
                return choose(job, progress, obs)
    return choose(job, progress, obs)


def _given_up(chooser) -> bool:
    return bool(getattr(chooser, "give_up", False))


def _absorb(job: Job, progress: Progress, obs: Observation) -> None:
    if obs.dialog or not ledger_is_open(obs.url):
        return
    if obs.sheet == "Vendors" and job.vendor_query and obs.search_query == job.vendor_query:
        count = len(obs.matching("Legal Name", job.vendor_query))
        if _expect_ok(job.vendor_expect, count):
            progress.judged["vendors"] = True
    if obs.sheet == "Purchase_Orders" and job.po_query and obs.search_query == job.po_query:
        count = len(obs.matching("PO ID", job.po_query))
        if _expect_ok(job.po_expect, count):
            progress.judged["po"] = True
    if obs.sheet == "Goods_Receipts" and job.grn_query and obs.search_query == job.grn_query:
        count = len(obs.matching("GRN ID", job.grn_query))
        if _expect_ok(job.grn_expect, count):
            progress.judged["grn"] = True


def _finish_if_done(job: Job, progress: Progress, obs: Observation) -> bool:
    if progress.outcome != "RUNNING":
        return True
    if not ledger_is_open(obs.url) or obs.dialog:
        return False
    if job.vendor_expect == "skip":
        _stop(progress, "READY_TO_VERIFY", "The document could not be read, so no ledger cells were edited.")
        return True
    if obs.sheet != "Invoice_Ledger":
        return False
    if job.invoice_id and obs.search_query != job.invoice_id:
        return False
    found = obs.matching("Invoice ID", job.invoice_id) if job.invoice_id else []
    if job.needs_clarification:
        if not progress.judged.get("vendors"):
            return False
        if found:
            _stop(progress, "UNVERIFIED", "The invoice is already on the ledger, so clarification cannot add another row.")
        else:
            progress.phase = "wait"
            progress.outcome = "WAITING_FOR_USER"
            progress.reason = "Several vendors match. Choose one before anything is written."
        return True
    if job.intended is None:
        if not progress.judged.get("vendors"):
            return False
        if job.duplicate_fixture and len(found) == 1:
            _stop(progress, "READY_TO_VERIFY", "The invoice is already on the ledger. No second row was added.")
            return True
        if not job.duplicate_fixture and len(found) == 0:
            _stop(progress, "READY_TO_VERIFY", "The rules do not allow a ledger row, and none was added.")
            return True
        return False
    if not (progress.judged.get("vendors") and progress.judged.get("po") and progress.judged.get("grn")):
        return False
    if (
        len(found) == 1
        and row_matches(found[0].cells, job.intended)
        and obs.save_state in {"saved", "unconfirmed"}
    ):
        _stop(progress, "READY_TO_VERIFY", "The grid shows the intended invoice.")
        return True
    return False


def _step_payload(
    phase: str,
    action: ComputerAction,
    obs: Observation,
    ok: bool,
    recovery: str,
    *,
    narrate: bool,
    duration_ms: int = 0,
    attempt: int = 0,
    plan: dict | None = None,
    adapted: dict | None = None,
    gate_reason: str = "",
) -> dict:
    payload = {
        "phase": phase,
        "goal": _narration(action) if narrate else action.goal,
        "action": action.action,
        "sheet": action.sheet or obs.sheet,
        "expected_result": action.expected_result,
        "observed": _observed(obs),
        "screenshot": obs.screenshot,
        "ok": ok,
        "recovery": recovery,
        "duration_ms": duration_ms,
        "attempt": attempt,
        "row": action.row,
        "column": action.column,
        "value": action.value,
        "search": action.search_value or "",
    }
    if phase == "rejected":
        payload["validation"] = "rejected"
        payload["gate_reason"] = gate_reason
    elif phase in {"about_to", "done"}:
        payload["validation"] = "accepted"
    if plan is not None:
        payload["plan"] = plan
        payload["control"] = plan.get("control") or ""
    if adapted:
        payload["sheet_adapted"] = adapted
    return payload


def _narration(action: ComputerAction) -> str:
    kind = action.action
    if kind == "open_workbook":
        return "Opening the ledger."
    if kind == "select_sheet":
        return f"Clicking {action.sheet}."
    if kind == "search_sheet":
        return f"Filtering for {action.search_value}."
    if kind == "add_row":
        return "Clicking Add row."
    if kind == "edit_cell":
        return f"Typing {action.column}."
    if kind == "save_workbook":
        return "Clicking Save."
    if kind == "dismiss_dialog":
        return "Closing the dialog."
    return action.goal


def choose(job: Job, progress: Progress, obs: Observation) -> ComputerAction | None:
    if progress.outcome != "RUNNING":
        return None
    if obs.dialog == "save_error":
        progress.save_attempts += 1
        if not within_budget(progress.save_attempts):
            _stop(progress, "FAILED", "The workbook rejected the save and the retry budget is spent.")
            return None
        progress.phase = "save"
        return _dialog("Dismiss the save error and try again")
    if obs.dialog == "lost_response":
        progress.phase = "readback"
        progress.judged["readback"] = False
        return _dialog("The confirmation was lost. Close it and look the invoice up.")
    if obs.dialog:
        progress.unexpected_attempts += 1
        if not within_budget(progress.unexpected_attempts):
            _stop(progress, "UNVERIFIED", f"An unexpected dialog stayed open: {obs.dialog}.")
            return None
        return _dialog("Close the unexpected dialog")

    phase = progress.phase
    if phase == "open":
        if not ledger_is_open(obs.url):
            return ComputerAction(
                goal="Open the ledger",
                action="open_workbook",
                expected_result="The ledger is open",
            )
        if job.vendor_expect == "skip":
            _stop(progress, "READY_TO_VERIFY", "The document could not be read, so no ledger cells were edited.")
            return None
        progress.phase = "vendors"
        return _sheet("Open the vendor sheet", "Vendors")
    if phase == "vendors":
        return _inspect(progress, obs, "vendors", "Vendors", "Legal Name", job.vendor_query, job.vendor_expect, "po", ("Legal Name", "Tax ID"))
    if phase == "po":
        return _inspect(progress, obs, "po", "Purchase_Orders", "PO ID", job.po_query, job.po_expect, "grn", ("PO ID", "Ordered Qty", "Unit Price"))
    if phase == "grn":
        return _inspect(progress, obs, "grn", "Goods_Receipts", "GRN ID", job.grn_query, job.grn_expect, "ledger", ("GRN ID", "PO ID", "Received Qty"))
    if phase == "ledger":
        return _ledger(job, progress, obs)
    if phase == "write":
        return _write(job, progress, obs)
    if phase == "save":
        if obs.save_state == "saved":
            progress.phase = "readback"
            progress.judged["readback"] = False
            return _search("Read the invoice back from the grid", job.invoice_id)
        return ComputerAction(
            goal="Save the workbook",
            action="save_workbook",
            expected_result="The page reports saved, a save error, or a lost confirmation",
        )
    if phase == "readback":
        if obs.sheet != "Invoice_Ledger":
            return _sheet("Return to the invoice ledger", "Invoice_Ledger")
        if obs.search_query != job.invoice_id:
            return _search("Filter the ledger to this invoice", job.invoice_id)
        if not progress.judged.get("readback"):
            progress.judged["readback"] = True
            found = obs.matching("Invoice ID", job.invoice_id)
            if len(found) == 1 and row_matches(found[0].cells, job.intended or {}):
                _stop(progress, "READY_TO_VERIFY", "The grid shows the intended invoice.")
                return None
            _stop(progress, "UNVERIFIED", "The visible ledger row does not match the intended invoice.")
        return None
    return None


def _inspect(
    progress: Progress,
    obs: Observation,
    key: str,
    sheet: str,
    column: str,
    query: str,
    expect: str,
    nxt: str,
    required: tuple[str, ...] = (),
) -> ComputerAction | None:
    label = _label_for(progress, obs, sheet, required, column, query)
    if label is None:
        _stop(progress, "UNVERIFIED", f"No {sheet} tab matched the headers and the target record.")
        return None
    if obs.sheet != label:
        return _sheet(f"Open {label}", label)
    if required and obs.columns and not set(required) <= set(obs.columns):
        progress.rejected_tabs.append(obs.sheet)
        return _inspect(progress, obs, key, sheet, column, query, expect, nxt, required)
    if query and obs.search_query != query:
        return _search(f"Search {label} for {query}", query)
    if not progress.judged.get(key):
        count = len(obs.matching(column, query)) if query else 0
        if not _expect_ok(expect, count):
            if label != sheet:
                progress.rejected_tabs.append(obs.sheet)
                return _inspect(progress, obs, key, sheet, column, query, expect, nxt, required)
            _stop(
                progress,
                "UNVERIFIED",
                f"{sheet} showed {count} rows for {query or 'blank'}, which does not match the books ({expect}).",
            )
            return None
        progress.judged[key] = True
        goal_name = {"vendors": "verify_vendor", "po": "verify_po", "grn": "verify_grn"}[key]
        progress.evidence[goal_name] = {
            "observation": _observed(obs),
            "decision": f"{goal_name} accepted {label} with {count} matching rows.",
            "evidence": f"Headers {', '.join(obs.columns)}. Filter {obs.search_query or 'none'}.",
        }
        if label != sheet and label not in progress.adaptations:
            progress.adaptations.append(label)
        progress.phase = nxt
    elif progress.phase != nxt:
        progress.phase = nxt
    return _sheet_for_phase(progress.phase, progress, obs)


def _ledger(job: Job, progress: Progress, obs: Observation) -> ComputerAction | None:
    if obs.sheet != "Invoice_Ledger":
        return _sheet("Open the invoice ledger", "Invoice_Ledger")
    if job.invoice_id and obs.search_query != job.invoice_id:
        return _search("Check whether this invoice is already on the ledger", job.invoice_id)
    if progress.judged.get("ledger"):
        return None
    progress.judged["ledger"] = True
    found = obs.matching("Invoice ID", job.invoice_id) if job.invoice_id else []
    if job.needs_clarification:
        if found:
            _stop(progress, "UNVERIFIED", "The invoice is already on the ledger, so clarification cannot add another row.")
            return None
        progress.phase = "wait"
        progress.outcome = "WAITING_FOR_USER"
        progress.reason = "Several vendors match. Choose one before anything is written."
        return None
    if job.intended is None:
        if job.duplicate_fixture and len(found) == 1:
            _stop(progress, "READY_TO_VERIFY", "The invoice is already on the ledger. No second row was added.")
            return None
        if not job.duplicate_fixture and len(found) == 0:
            _stop(progress, "READY_TO_VERIFY", "The rules do not allow a ledger row, and none was added.")
            return None
        _stop(progress, "UNVERIFIED", "The ledger does not match the decision to leave it unchanged.")
        return None
    if len(found) == 1 and job.intended:
        kind = _row_kind(found[0].cells, job.intended)
        if kind == "match":
            progress.evidence["check_duplicate"] = {
                "observation": _observed(obs),
                "decision": "The saved row matches the intended invoice.",
                "evidence": "A fresh read of the ledger found one matching row.",
            }
            _stop(progress, "READY_TO_VERIFY", "This invoice is already stored with the intended values.")
            return None
        if kind == "partial":
            progress.blocked_on = "partial_row"
            progress.pending_question = "A partial row needs review before anything else is written."
            _stop(progress, "NEEDS_RECOVERY", "A partial ledger row exists for this invoice. I stopped so I would not add another row or fill in the rest.")
            return None
        progress.blocked_on = "conflict"
        progress.pending_question = "The existing row conflicts with the intended invoice."
        _stop(progress, "NEEDS_RECOVERY", "A ledger row for this invoice conflicts with the intended values. It needs review.")
        return None
    if found:
        _stop(progress, "UNVERIFIED", "A ledger row for this invoice exists and does not match the intended values.")
        return None
    progress.phase = "write"
    return _sheet("Clear the filter before typing the new row", "Invoice_Ledger")


def _write(job: Job, progress: Progress, obs: Observation) -> ComputerAction | None:
    intended = job.intended or {}
    if obs.sheet != "Invoice_Ledger" or obs.search_query:
        return _sheet("Show the full invoice ledger", "Invoice_Ledger")
    if progress.write_index is None:
        blank = next((row for row in obs.rows if not row.cells.get("Invoice ID")), None)
        if blank is None:
            return ComputerAction(
                goal="Add a blank ledger row",
                action="add_row",
                sheet="Invoice_Ledger",
                expected_result="A blank ledger row is visible",
            )
        progress.write_index = blank.index
    if progress.column_index >= len(LEDGER):
        progress.phase = "save"
        return ComputerAction(
            goal="Save the new ledger row",
            action="save_workbook",
            expected_result="The page reports saved, a save error, or a lost confirmation",
        )
    column = LEDGER[progress.column_index]
    wanted = intended.get(column, "")
    current = _cell(obs, progress.write_index, column)
    if _values_match(current, wanted):
        progress.column_index += 1
        return _write(job, progress, obs)
    return ComputerAction(
        goal=f"Enter {column}",
        action="edit_cell",
        sheet="Invoice_Ledger",
        row=progress.write_index,
        column=column,
        value=wanted,
        expected_result=f"{column} shows the intended value",
    )


def _sheet_for_phase(phase: str, progress: Progress, obs: Observation) -> ComputerAction | None:
    if phase == "po":
        label = _label_for(progress, obs, "Purchase_Orders", ("PO ID", "Ordered Qty", "Unit Price"), "PO ID", "")
        if label is None:
            _stop(progress, "UNVERIFIED", "No purchase-order tab matched the headers and the target record.")
            return None
        return _sheet("Open purchase orders" if label == "Purchase_Orders" else f"Open {label}", label)
    if phase == "grn":
        label = _label_for(progress, obs, "Goods_Receipts", ("GRN ID", "PO ID", "Received Qty"), "GRN ID", "")
        if label is None:
            _stop(progress, "UNVERIFIED", "No goods-receipt tab matched the headers and the target record.")
            return None
        return _sheet("Open goods receipts" if label == "Goods_Receipts" else f"Open {label}", label)
    if phase == "ledger":
        return _sheet("Open the invoice ledger", "Invoice_Ledger")
    return None


def _sheet(goal: str, sheet: str) -> ComputerAction:
    return ComputerAction(
        goal=goal,
        action="select_sheet",
        sheet=sheet,
        expected_result=f"The {sheet} sheet is showing",
    )


def _search(goal: str, value: str) -> ComputerAction:
    return ComputerAction(
        goal=goal,
        action="search_sheet",
        search_value=value,
        expected_result=f"The filter shows {value}",
    )


def _dialog(goal: str) -> ComputerAction:
    return ComputerAction(
        goal=goal,
        action="dismiss_dialog",
        expected_result="The dialog is closed",
    )


def _expect_ok(expect: str, count: int) -> bool:
    if expect == "one":
        return count == 1
    if expect == "many":
        return count >= 2
    if expect == "none":
        return count == 0
    return False


def _cell(obs: Observation, index: int, column: str) -> str:
    for row in obs.rows:
        if row.index == index:
            return row.cells.get(column, "")
    return ""


def _values_match(current: str, wanted: str) -> bool:
    if current == wanted:
        return True
    if not current and not wanted:
        return True
    try:
        return float(current) == float(wanted)
    except ValueError:
        return False


def _stop(progress: Progress, outcome: str, reason: str) -> None:
    progress.phase = "done"
    progress.outcome = outcome
    progress.reason = reason


def _label_for(
    progress: Progress,
    obs: Observation,
    canonical: str,
    required: tuple[str, ...],
    column: str,
    query: str,
) -> str | None:
    if canonical not in progress.rejected_tabs:
        return canonical
    for tab in obs.tabs:
        if tab.label in progress.rejected_tabs:
            continue
        if required and not set(required) <= set(tab.headers):
            continue
        if query and tab.rows and not _tab_has(tab, column, query):
            continue
        return tab.label
    return None


def _tab_has(tab, column: str, query: str) -> bool:
    needle = query.casefold()
    for row in tab.rows:
        if needle in str(row.get(column, "")).casefold():
            return True
    return False


def _row_kind(actual: dict[str, str], intended: dict[str, str]) -> str:
    if row_matches(actual, intended):
        return "match"
    conflict = False
    missing = False
    for column, wanted in intended.items():
        if column == "Processed At":
            continue
        seen = actual.get(column, "")
        if _values_match(seen, wanted):
            continue
        if not seen:
            missing = True
        else:
            conflict = True
    if conflict:
        return "conflict"
    if missing:
        return "partial"
    return "conflict"


def _remember_visible(job: Job, progress: Progress, obs: Observation) -> None:
    if job.invoice_id and obs.sheet == "Invoice_Ledger" and obs.search_query == job.invoice_id:
        found = obs.matching("Invoice ID", job.invoice_id)
        if len(found) == 1 and job.intended and row_matches(found[0].cells, job.intended):
            progress.evidence["verify_persistence"] = {
                "observation": _observed(obs),
                "decision": "The ledger row matches the intended invoice.",
                "evidence": f"Save state {obs.save_state}.",
            }


def _note_handoff(progress: Progress, chooser, on_step, job: Job) -> None:
    if chooser is None or not _given_up(chooser) or progress.handed_off:
        return
    progress.handed_off = True
    progress.control = "deterministic_recovery"
    if on_step is None:
        return
    on_step(
        {
            "phase": "handoff",
            "goal": "Model action rejected twice. Switching to deterministic recovery. Financial rules remain enforced.",
            "action": "control_handoff",
            "sheet": "",
            "expected_result": "Deterministic recovery continues under the financial rules.",
            "observed": "",
            "screenshot": None,
            "ok": True,
            "recovery": getattr(chooser, "last_error", "") or "",
            "duration_ms": 0,
            "attempt": 0,
            "control": progress.control,
            "plan": _plan_snapshot(job, progress),
        }
    )


def _adaptation_payload(progress: Progress) -> dict | None:
    if not progress.adaptations or progress.adaptation_reported:
        return None
    if "verify_po" not in progress.evidence and "verify_grn" not in progress.evidence and "verify_vendor" not in progress.evidence:
        return None
    progress.adaptation_reported = True
    label = progress.adaptations[-1]
    evidence = progress.evidence.get("verify_po") or progress.evidence.get("verify_grn") or progress.evidence.get("verify_vendor") or {}
    return {"label": label, "observation": evidence.get("observation", ""), "evidence": evidence.get("evidence", "")}


def _plan_snapshot(job: Job, progress: Progress) -> dict:
    order = ["verify_vendor", "verify_po", "verify_grn", "check_duplicate"]
    if job.intended is not None and not job.needs_clarification:
        order.extend(["write_ledger", "verify_persistence"])
    if job.vendor_expect == "skip":
        order = []
    phase_at = {
        "open": -1,
        "vendors": 0,
        "po": 1,
        "grn": 2,
        "ledger": 3,
        "write": 4,
        "save": 5,
        "readback": 5,
        "wait": 3,
        "done": len(order),
    }.get(progress.phase, -1)
    if progress.outcome in {"READY_TO_VERIFY", "NEEDS_RECOVERY", "UNVERIFIED", "FAILED", "WAITING_FOR_USER"}:
        phase_at = len(order)
    completed = [name for name in order[: max(phase_at, 0)] if phase_at > 0 or progress.outcome != "RUNNING"]
    if progress.outcome == "RUNNING":
        completed = order[: max(phase_at, 0)]
    else:
        completed = [name for name in order if name in progress.evidence or (name == "check_duplicate" and progress.judged.get("ledger"))]
        if progress.outcome == "READY_TO_VERIFY" and job.intended is not None and "verify_persistence" not in completed:
            if progress.evidence.get("verify_persistence") or progress.phase == "done":
                completed = [*order]
    current = None
    if progress.outcome == "RUNNING" and 0 <= phase_at < len(order):
        current = order[phase_at]
    pending = [name for name in order if name not in completed and name != current]
    if job.intended and "NEEDS_APPROVAL" == (job.intended or {}).get("Status"):
        success = "A verified NEEDS_APPROVAL row exists on the ledger"
    elif job.intended:
        success = "A verified review row exists on the ledger"
    elif job.vendor_expect == "skip":
        success = "No ledger row is written"
    else:
        success = "The ledger is unchanged and the reason is recorded"
    invoice = job.invoice_id or "the invoice"
    return {
        "task_goal": f"Process {invoice} safely",
        "success_condition": success,
        "completed": completed,
        "current_goal": current,
        "pending": pending,
        "blocked_on": progress.blocked_on,
        "pending_question": progress.pending_question,
        "evidence": progress.evidence,
        "control": progress.control,
        "retry_count": progress.action_attempts,
        "last_confirmed_action": progress.records[-1].action if progress.records else None,
    }


def _record(action: ComputerAction, after: Observation, ok: bool, recovery: str) -> StepRecord:
    return StepRecord(
        goal=action.goal,
        action=action.action,
        sheet=after.sheet,
        expected_result=action.expected_result,
        observed=_observed(after),
        ok=ok,
        recovery=recovery,
        screenshot=after.screenshot,
    )


def _observed(obs: Observation) -> str:
    parts = [f"sheet {obs.sheet or 'none'}", f"save {obs.save_state}"]
    if obs.dialog:
        parts.append(f"dialog {obs.dialog}")
    if obs.search_query:
        parts.append(f"filter {obs.search_query}")
    parts.append(f"{len(obs.rows)} visible rows")
    return ", ".join(parts)
