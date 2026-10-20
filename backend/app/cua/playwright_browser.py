"""Drive the finance workbook with one action at a time.

Screenshots are taken from the live page. This module does not invent them.
Actions use semantic Playwright locators. Visible mode may animate a cursor
for demos, but the click itself stays on the locator (not coordinates).
A shared session can keep Chromium open across tasks.
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.cua.schemas import COLUMNS, ComputerAction, GridRow, Observation
from app.cua.workbook_io import reset_workbook

VISIBLE_PACE_MS = 70
VISIBLE_TYPE_DELAY_MS = 8
VISIBLE_DWELL_MS = 280

_CURSOR_JS = """
() => {
  if (document.getElementById("agent-cursor")) return;
  const cursor = document.createElement("div");
  cursor.id = "agent-cursor";
  cursor.setAttribute("aria-hidden", "true");
  cursor.style.position = "fixed";
  cursor.style.zIndex = "100000";
  cursor.style.width = "14px";
  cursor.style.height = "14px";
  cursor.style.marginLeft = "-7px";
  cursor.style.marginTop = "-7px";
  cursor.style.borderRadius = "99px";
  cursor.style.border = "2px solid #16382c";
  cursor.style.background = "#f4f7f5";
  cursor.style.boxShadow = "0 1px 4px rgba(0,0,0,0.35)";
  cursor.style.pointerEvents = "none";
  cursor.style.left = "24px";
  cursor.style.top = "24px";
  document.body.appendChild(cursor);
  document.addEventListener("mousemove", (event) => {
    cursor.style.left = event.clientX + "px";
    cursor.style.top = event.clientY + "px";
  }, true);
}
"""


class PlaywrightBrowser:
    def __init__(
        self,
        origin: str,
        shot_dir: Path,
        *,
        headless: bool = False,
        faults_enabled: bool = False,
        workbook_path: Path | None = None,
        persistent: bool = False,
    ) -> None:
        self.origin = origin.rstrip("/")
        self.shot_dir = shot_dir
        self.headless = headless
        self.faults_enabled = faults_enabled
        self.workbook_path = workbook_path
        self.persistent = persistent
        self.fault: str | None = None
        self.pace_ms = 0 if headless else VISIBLE_PACE_MS
        self._playwright = None
        self._browser = None
        self._page = None
        self._shot_index = 0
        self.last_open_ms = 0.0
        self.last_act_ms = 0.0

    def bind_task(self, shot_dir: Path) -> None:
        self.shot_dir = shot_dir
        self._shot_index = 0
        self.shot_dir.mkdir(parents=True, exist_ok=True)

    def open(self, fault: str | None = None, *, reset: bool = False) -> None:
        t0 = time.perf_counter()
        if reset and self.workbook_path is not None:
            reset_workbook(self.workbook_path)
        self.fault = fault if self.faults_enabled else None
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        if self._page is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless, channel="chrome")
            self._page = self._browser.new_page(viewport={"width": 1280, "height": 800})
            self._page.set_default_timeout(10_000)
        elif reset:
            # Disk seed changed; drop the stale SPA so the next open_workbook reloads.
            try:
                self._page.goto("about:blank")
            except Exception:
                pass
        if not self.headless:
            self._page.bring_to_front()
        self.last_open_ms = (time.perf_counter() - t0) * 1000

    def close(self, *, shutdown: bool = False) -> None:
        if self.persistent and not shutdown:
            if self._page is not None:
                try:
                    if self.pace_ms:
                        self._page.bring_to_front()
                        self._page.wait_for_timeout(VISIBLE_DWELL_MS)
                    # Leave a blank page so the next task always runs open_workbook.
                    self._page.goto("about:blank")
                except Exception:
                    pass
            return
        if self._page is not None and self.pace_ms:
            try:
                self._page.bring_to_front()
                self._page.wait_for_timeout(VISIBLE_DWELL_MS)
            except Exception:
                pass
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._browser = None
        self._page = None
        self._playwright = None

    def observe(self, *, capture: bool = True) -> Observation:
        page = self._page
        if page is None:
            return Observation(url="")
        url = page.url
        if "/workbook" in url:
            try:
                page.wait_for_function(
                    "() => window.ledgerGrid && window.ledgerGrid.ready",
                    timeout=5_000,
                )
            except Exception:
                shot = self._shoot() if capture else None
                return Observation(url=url, screenshot=shot)
        shot = self._shoot() if capture else None
        if "/workbook" not in url:
            return Observation(url=url, screenshot=shot)
        data = page.evaluate("() => window.ledgerGrid.snapshot()")
        rows = [
            GridRow(index=int(row["index"]), cells={key: str(value) for key, value in row["cells"].items()})
            for row in data["rows"]
        ]
        return Observation(
            url=url,
            sheet=data["sheet"],
            dialog=data["dialog"],
            save_state=data["saveState"],
            search_query=data["searchQuery"],
            columns=list(data["columns"]),
            rows=rows,
            row_count=int(data["rowCount"]),
            screenshot=shot,
        )

    def act(self, action: ComputerAction) -> None:
        page = self._page
        if page is None:
            raise RuntimeError("The browser is not open.")
        timeout = action.timeout_ms
        kind = action.action
        t0 = time.perf_counter()
        self._announce(action.goal)
        if kind == "open_workbook":
            target = self._workbook_url()
            if not target.startswith(self.origin + "/"):
                raise RuntimeError("Refusing to open a page outside the ledger origin.")
            page.goto(target, timeout=timeout, wait_until="domcontentloaded")
            page.wait_for_function("() => window.ledgerGrid && window.ledgerGrid.ready", timeout=timeout)
            self._arm()
            self._announce(action.goal)
            self.last_act_ms = (time.perf_counter() - t0) * 1000
            return
        if kind == "select_sheet":
            self._press(page.locator(f'[data-sheet-tab="{action.sheet}"]'), timeout)
            page.wait_for_function(
                "(sheet) => window.ledgerGrid && window.ledgerGrid.ready && window.ledgerGrid.snapshot().sheet === sheet",
                arg=action.sheet,
                timeout=timeout,
            )
            self.last_act_ms = (time.perf_counter() - t0) * 1000
            return
        if kind == "search_sheet":
            wanted = action.search_value or ""
            self._type_into(page.locator("#sheet-search"), wanted, timeout)
            self._press(page.locator("#sheet-search-go"), timeout)
            page.wait_for_function(
                "(q) => window.ledgerGrid && window.ledgerGrid.snapshot().searchQuery === q",
                arg=wanted,
                timeout=timeout,
            )
            self.last_act_ms = (time.perf_counter() - t0) * 1000
            return
        if kind == "add_row":
            before = page.evaluate("() => window.ledgerGrid.snapshot().rowCount")
            self._press(page.locator("#add-row"), timeout)
            page.wait_for_function(
                "(n) => window.ledgerGrid && window.ledgerGrid.snapshot().rowCount > n",
                arg=before,
                timeout=timeout,
            )
            self.last_act_ms = (time.perf_counter() - t0) * 1000
            return
        if kind == "edit_cell":
            column = action.column or ""
            if column not in COLUMNS["Invoice_Ledger"]:
                raise RuntimeError("Refusing to edit a column outside the invoice ledger.")
            self._press(page.locator(f'td[data-row="{action.row}"][data-column="{column}"]'), timeout)
            formula = page.locator("#formula")
            self._type_into(formula, action.value or "", timeout)
            formula.press("Enter")
            wanted = action.value or ""
            page.wait_for_function(
                """({row, column, wanted}) => {
                    const snap = window.ledgerGrid && window.ledgerGrid.snapshot();
                    if (!snap) return false;
                    const hit = snap.rows.find((item) => item.index === row);
                    return hit && String(hit.cells[column] ?? "") === wanted;
                }""",
                arg={"row": action.row, "column": column, "wanted": wanted},
                timeout=timeout,
            )
            self.last_act_ms = (time.perf_counter() - t0) * 1000
            return
        if kind == "save_workbook":
            self._press(page.locator("#save-book"), timeout)
            page.wait_for_function(
                """() => {
                    const snap = window.ledgerGrid && window.ledgerGrid.snapshot();
                    if (!snap) return false;
                    return ['saved', 'error', 'unconfirmed'].includes(snap.saveState) || Boolean(snap.dialog);
                }""",
                timeout=timeout,
            )
            self.last_act_ms = (time.perf_counter() - t0) * 1000
            return
        if kind == "dismiss_dialog":
            self._press(page.locator("#dialog-ok"), timeout)
            page.wait_for_function(
                "() => window.ledgerGrid && !window.ledgerGrid.snapshot().dialog",
                timeout=timeout,
            )
            self.last_act_ms = (time.perf_counter() - t0) * 1000
            return
        raise RuntimeError(kind)

    def _announce(self, message: str) -> None:
        page = self._page
        if page is None or not message or "/workbook" not in page.url:
            return
        page.evaluate(
            "(message) => { if (window.ledgerGrid) window.ledgerGrid.setActivity(message); }",
            message,
        )

    def _workbook_url(self) -> str:
        parts: list[str] = []
        if self.fault:
            parts.append(f"fault={self.fault}")
        if self.pace_ms:
            parts.append("agent=1")
        query = f"?{'&'.join(parts)}" if parts else ""
        return f"{self.origin}/workbook{query}"

    def _arm(self) -> None:
        page = self._page
        if page is None:
            return
        if not self.headless:
            page.bring_to_front()
        if self.pace_ms:
            page.evaluate(_CURSOR_JS)

    def _press(self, locator, timeout: int) -> None:
        """Semantic click. Optional cursor motion is decorative only."""
        locator.scroll_into_view_if_needed(timeout=timeout)
        page = self._page
        if self.pace_ms and page is not None:
            self._arm()
            box = locator.bounding_box()
            if box is not None:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y, steps=6)
        locator.click(timeout=timeout)
        if self.pace_ms:
            locator.evaluate(
                """(el) => {
                    const previous = el.style.outline;
                    const offset = el.style.outlineOffset;
                    el.style.outline = "3px solid #16382c";
                    el.style.outlineOffset = "2px";
                    setTimeout(() => {
                        el.style.outline = previous;
                        el.style.outlineOffset = offset;
                    }, 240);
                }"""
            )

    def _type_into(self, locator, text: str, timeout: int) -> None:
        self._press(locator, timeout)
        if not self.pace_ms:
            locator.fill(text, timeout=timeout)
            return
        locator.fill("", timeout=timeout)
        if text and self._page is not None:
            self._page.keyboard.type(text, delay=VISIBLE_TYPE_DELAY_MS)

    def _shoot(self) -> str | None:
        page = self._page
        if page is None:
            return None
        self._shot_index += 1
        path = self.shot_dir / f"{self._shot_index}.png"
        page.screenshot(path=str(path))
        task_id = self.shot_dir.name
        return f"/api/cua/shots/{task_id}/{path.name}"
