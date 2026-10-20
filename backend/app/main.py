import json
import os
import queue
import re
import threading
import uuid
from dataclasses import replace
from collections.abc import Iterator
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.ask import ask_about_run
from app.chat import execute_chat, progress_lines, resume_pending
from app.accounting import (
    AccountingStore,
    DuplicateInvoice,
    InvalidTransition,
    SaveFailed,
    draft_from_payload,
)
from app.config import ROOT, Settings
from app.cua.sql_ledger import LedgerBook, SqlLedgerBrowser, sheets_payload
from app.cua.workbook_io import DEFAULT_WORKBOOK, ensure_workbook, sheets_as_rows, write_sheets
from app.cua.worker import run_workbook_task
from app.db import connect, init_db
from app.documents import (
    CATALOG,
    invoice_pdf_path,
    pack_file,
    pack_filenames,
    pdf_preview,
    task_pdf_path,
)
from app.extractor import ExtractionError, GeminiExtractor, GroqExtractor, TemplateExtractor
from app.orchestrator import Orchestrator
from app.schemas import TaskOut
from app.seed import seed

HIDDEN_CENTS = {"subtotal_cents", "tax_cents", "total_cents", "tax_rate_bps"}
DIST = ROOT / "frontend" / "dist"


def create_app(
    db_path: Path | None = None,
    extractor: object | None = None,
    *,
    workbook_path: Path | None = None,
) -> FastAPI:
    settings = Settings.from_env()
    conn = connect(db_path or settings.db_path)
    init_db(conn)
    seed(conn)
    store = AccountingStore(conn)
    chosen = extractor if extractor is not None else _extractor(settings)
    orchestrator = Orchestrator(store, chosen)
    books = workbook_path or DEFAULT_WORKBOOK
    ensure_workbook(books)
    app = FastAPI(title="Ledger Agent")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = store
    app.state.orchestrator = orchestrator
    app.state.settings = settings
    app.state.workbook_path = books
    app.state.cua_browser_factory = None
    app.state.cua_browser = None
    app.state.cua_origin = "http://127.0.0.1:8000"
    app.state.ledger = LedgerBook(conn)

    @app.on_event("shutdown")
    def _shutdown_cua_browser() -> None:
        browser = getattr(app.state, "cua_browser", None)
        if browser is not None:
            browser.close(shutdown=True)
            app.state.cua_browser = None

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "model": settings.model,
            "cua_headless": os.environ.get("CUA_HEADLESS", "0") == "1",
        }

    @app.get("/api/vendors")
    def vendors() -> list[dict]:
        return store.list_vendors()

    @app.get("/api/purchase-orders")
    def purchase_orders() -> list[dict]:
        return store.list_purchase_orders()

    @app.get("/api/goods-receipts")
    def goods_receipts() -> list[dict]:
        return store.list_goods_receipts()

    @app.get("/api/invoices")
    def invoices() -> list[dict]:
        return [_public_invoice(row) for row in store.list_invoices()]

    @app.get("/api/invoices/{invoice_id}")
    def invoice_detail(invoice_id: int) -> dict:
        row = store.get_invoice(invoice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        payload = _public_invoice(row)
        if row["task_id"]:
            task = store.task_by_id(row["task_id"])
            payload["task"] = orchestrator._view(task) if task else None
        else:
            payload["task"] = None
        return payload

    @app.post("/api/invoices/{invoice_id}/approve")
    def approve(invoice_id: int) -> dict:
        return _review(store, invoice_id, "approved", store.approve)

    @app.post("/api/invoices/{invoice_id}/reject")
    def reject(invoice_id: int) -> dict:
        return _review(store, invoice_id, "rejected", store.reject)

    @app.get("/api/ledger")
    def ledger() -> dict:
        return sheets_payload(conn)

    @app.post("/api/ledger/approve")
    def approve_ledger(payload: dict = Body(...)) -> dict:
        invoice_id = str(payload.get("invoice_id") or "").strip()
        if not invoice_id:
            raise HTTPException(status_code=400, detail="Name the invoice to approve.")
        try:
            result = app.state.ledger.approve(invoice_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        task = _task_for_sheet_invoice(store, invoice_id)
        if task is not None:
            store.add_audit(
                task["id"],
                "human_review",
                "approved",
                {"invoice_id": invoice_id, "source": "ledger", "changed": result.get("changed")},
            )
        return result

    @app.post("/api/ledger/reset")
    def reset_ledger() -> dict:
        app.state.ledger.reset_to_seed()
        return sheets_payload(conn)

    @app.get("/api/tasks")
    def tasks() -> list[dict]:
        return [orchestrator._view(task) for task in store.list_tasks()]

    @app.get("/api/tasks/{task_id}", response_model=TaskOut)
    def task_detail(task_id: str) -> dict:
        task = store.task_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        return orchestrator._view(task)

    @app.post("/api/tasks", response_model=TaskOut)
    async def create_task(
        instruction: str = Form(...),
        idempotency_key: str = Form(""),
        file: UploadFile = File(...),
    ) -> dict:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="The invoice file is empty.")
        if len(payload) > 10_000_000:
            raise HTTPException(status_code=400, detail="The invoice file is larger than 10 MB.")
        key = idempotency_key.strip() or str(uuid.uuid4())
        try:
            return orchestrator.run(
                instruction=instruction.strip(),
                filename=file.filename or "invoice.pdf",
                pdf_bytes=payload,
                idempotency_key=key,
            )
        except ExtractionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/tasks/stream")
    async def stream_task(
        instruction: str = Form(...),
        idempotency_key: str = Form(""),
        file: UploadFile = File(...),
    ) -> StreamingResponse:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="The invoice file is empty.")
        if len(payload) > 10_000_000:
            raise HTTPException(status_code=400, detail="The invoice file is larger than 10 MB.")
        key = idempotency_key.strip() or str(uuid.uuid4())
        filename = file.filename or "invoice.pdf"
        return StreamingResponse(
            _task_events(orchestrator, instruction.strip(), filename, payload, key),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/ask")
    def ask(payload: dict = Body(...)) -> dict:
        question = str(payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="Ask a question first.")
        if len(question) > 2000:
            raise HTTPException(status_code=400, detail="That question is too long.")
        payload["question"] = question
        try:
            return ask_about_run(settings, payload)
        except ExtractionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/cua/config")
    def cua_config() -> dict:
        return {
            "faults": os.environ.get("CUA_FAULTS") == "1",
            "destination": "ledger",
            "headless": os.environ.get("CUA_HEADLESS", "0") == "1",
        }

    @app.get("/api/workbook")
    def workbook() -> dict:
        ensure_workbook(books)
        return {"sheets": sheets_as_rows(books)}

    @app.post("/api/workbook")
    def save_workbook(payload: dict = Body(...)) -> dict:
        sheets = payload.get("sheets")
        if not isinstance(sheets, dict):
            raise HTTPException(status_code=400, detail="The workbook payload needs sheets.")
        try:
            write_sheets(books, sheets)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/chat/stream")
    async def stream_chat(
        message: str = Form(...),
        task_id: str = Form(""),
        file: UploadFile | None = File(None),
        files: list[UploadFile] | None = File(None),
    ) -> StreamingResponse:
        text = message.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Say what you want done.")
        if len(text) > 2000:
            raise HTTPException(status_code=400, detail="That message is too long.")
        incoming: list[UploadFile] = []
        if file is not None and file.filename:
            incoming.append(file)
        for item in files or []:
            if item is not None and item.filename:
                incoming.append(item)
        uploads: list[tuple[str, bytes]] = []
        for item in incoming:
            payload = await item.read()
            if not payload:
                continue
            if len(payload) > 10_000_000:
                raise HTTPException(status_code=400, detail="An invoice file is larger than 10 MB.")
            uploads.append((item.filename or "invoice.pdf", payload))
        prior_id = task_id.strip()
        return StreamingResponse(
            _chat_events(app, text, uploads, prior_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/chat/resume")
    def resume_chat(payload: dict = Body(...)) -> dict:
        task_id = str(payload.get("task_id") or "").strip()
        task = store.task_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        return resume_pending(task=task, settings=settings, workbook_path=books)

    @app.post("/api/tasks/cua/stream")
    async def stream_cua_task(
        instruction: str = Form(...),
        idempotency_key: str = Form(""),
        file: UploadFile = File(...),
    ) -> StreamingResponse:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="The invoice file is empty.")
        if len(payload) > 10_000_000:
            raise HTTPException(status_code=400, detail="The invoice file is larger than 10 MB.")
        key = idempotency_key.strip() or str(uuid.uuid4())
        filename = file.filename or "invoice.pdf"
        return StreamingResponse(
            _cua_events(app, instruction.strip(), filename, payload, key, None),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/tasks/{task_id}/clarify")
    def clarify(task_id: str, payload: dict = Body(...)) -> dict:
        choice = str(payload.get("vendor_name") or "").strip()
        if not choice:
            raise HTTPException(status_code=400, detail="Choose a vendor.")
        task = store.task_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        pdf_path = Path(task_pdf_path(task_id))
        if not pdf_path.exists():
            raise HTTPException(status_code=404, detail="The invoice file for this task is gone.")
        try:
            return run_workbook_task(
                store=store,
                extractor=chosen,
                workbook_path=books,
                browser_factory=lambda task_id, app=app: _cua_browser(app, task_id),
                origin=app.state.cua_origin,
                instruction=task["instruction"],
                filename=task["filename"] or "invoice.pdf",
                pdf_bytes=pdf_path.read_bytes(),
                idempotency_key=task["idempotency_key"],
                on_event=None,
                vendor_choice=choice,
                settings=_live_settings(settings, chosen),
            )
        except ExtractionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/workbook")
    def workbook_page() -> FileResponse:
        page = ROOT / "backend" / "app" / "cua" / "static" / "workbook.html"
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    @app.get("/workbook/app.js")
    def workbook_script() -> FileResponse:
        return FileResponse(
            ROOT / "backend" / "app" / "cua" / "static" / "workbook.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/workbook/app.css")
    def workbook_style() -> FileResponse:
        return FileResponse(
            ROOT / "backend" / "app" / "cua" / "static" / "workbook.css",
            media_type="text/css",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/cua/shots/{task_id}/{name}")
    def cua_shot(task_id: str, name: str) -> FileResponse:
        if not re.fullmatch(r"[0-9a-f-]{36}", task_id) or not re.fullmatch(r"\d+\.png", name):
            raise HTTPException(status_code=404, detail="Screenshot not found.")
        path = books.parent / "cua_shots" / task_id / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Screenshot not found.")
        return FileResponse(path)

    @app.post("/api/accounting/invoices")
    def create_accounting_invoice(payload: dict) -> dict:
        try:
            draft = draft_from_payload(payload)
            saved, created = store.create_invoice(draft)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DuplicateInvoice as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SaveFailed as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"created": created, "invoice": _public_invoice(saved)}

    @app.get("/api/accounting/invoices/{invoice_id}")
    def accounting_invoice(invoice_id: int) -> dict:
        row = store.get_invoice(invoice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        return _public_invoice(row)

    @app.get("/api/tasks/{task_id}/pdf")
    def task_pdf(task_id: str) -> FileResponse:
        task = store.task_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        path = Path(task_pdf_path(task_id))
        if not path.exists():
            raise HTTPException(status_code=404, detail="This task has no PDF.")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=task["filename"] or "invoice.pdf",
            content_disposition_type="inline",
        )

    @app.get("/api/invoices/{invoice_id}/pdf")
    def invoice_pdf_file(invoice_id: int) -> FileResponse:
        row = store.get_invoice(invoice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        path = _pdf_for_invoice(row)
        if path is None:
            raise HTTPException(status_code=404, detail="This invoice has no PDF.")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"{row['invoice_number']}.pdf",
            content_disposition_type="inline",
        )

    @app.post("/api/preview")
    async def preview_upload(file: UploadFile = File(...)) -> Response:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="The invoice file is empty.")
        try:
            image = pdf_preview(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Could not read that PDF.") from exc
        return Response(content=image, media_type="image/png")

    @app.get("/api/invoices/{invoice_id}/preview")
    def invoice_preview(invoice_id: int) -> Response:
        row = store.get_invoice(invoice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        path = _pdf_for_invoice(row)
        if path is None:
            raise HTTPException(status_code=404, detail="This invoice has no PDF.")
        return Response(content=pdf_preview(path.read_bytes()), media_type="image/png")

    @app.get("/api/samples")
    def samples() -> list[dict]:
        rows = []
        for name in pack_filenames():
            spec = CATALOG.get(name)
            rows.append(
                {
                    "name": name,
                    "vendor": spec.vendor_name if spec is not None else "Unreadable scan",
                    "invoice_number": spec.invoice_number if spec is not None else "",
                }
            )
        return rows

    @app.get("/api/samples/{name}")
    def sample_file(name: str) -> FileResponse:
        path = pack_file(name)
        if path is None:
            raise HTTPException(status_code=404, detail="Sample not found.")
        return FileResponse(path, media_type="application/pdf", filename=name, content_disposition_type="inline")

    _install_frontend(app)
    return app


def _pdf_for_invoice(row: dict) -> Path | None:
    for spec in CATALOG.values():
        if spec.invoice_number == row["invoice_number"] and spec.vendor_name == row["vendor_name"]:
            packed = pack_file(spec.filename)
            if packed is not None:
                return packed
    if row["task_id"]:
        task_path = Path(task_pdf_path(row["task_id"]))
        if task_path.exists():
            return task_path
    invoice_path = Path(invoice_pdf_path(int(row["id"])))
    if invoice_path.exists():
        return invoice_path
    return None


def _cua_browser(app: FastAPI, task_id: str):
    factory = app.state.cua_browser_factory
    if factory is not None:
        return factory(task_id)
    shared = getattr(app.state, "cua_browser", None)
    if shared is None:
        shared = SqlLedgerBrowser(app.state.store.conn)
        app.state.cua_browser = shared
    return shared


def _cua_events(app: FastAPI, instruction: str, filename: str, payload: bytes, key: str, vendor_choice: str | None) -> Iterator[str]:
    events: queue.Queue[tuple[str, dict] | None] = queue.Queue()

    def on_event(event: dict) -> None:
        events.put(("audit", event))

    def work() -> None:
        try:
            result = run_workbook_task(
                store=app.state.store,
                extractor=app.state.orchestrator.extractor,
                workbook_path=app.state.workbook_path,
                browser_factory=lambda task_id: _cua_browser(app, task_id),
                origin=app.state.cua_origin,
                instruction=instruction,
                filename=filename,
                pdf_bytes=payload,
                idempotency_key=key,
                on_event=on_event,
                vendor_choice=vendor_choice,
                settings=_live_settings(app.state.settings, app.state.orchestrator.extractor),
            )
            events.put(("done", result))
        except Exception as exc:
            events.put(("error", {"detail": str(exc)}))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()
    while True:
        item = events.get()
        if item is None:
            break
        kind, body = item
        yield f"event: {kind}\ndata: {json.dumps(body)}\n\n"


def _chat_events(app: FastAPI, message: str, uploads: list[tuple[str, bytes]], prior_id: str) -> Iterator[str]:
    events: queue.Queue[tuple[str, dict] | None] = queue.Queue()

    def on_event(event: dict) -> None:
        events.put(("audit", event))

    def work() -> None:
        try:
            prior = app.state.store.task_by_id(prior_id) if prior_id else None

            def run_invoice(filename: str, payload: bytes) -> dict:
                return run_workbook_task(
                    store=app.state.store,
                    extractor=app.state.orchestrator.extractor,
                    workbook_path=app.state.workbook_path,
                    browser_factory=lambda task_id: _cua_browser(app, task_id),
                    origin=app.state.cua_origin,
                    instruction=message,
                    filename=filename or "invoice.pdf",
                    pdf_bytes=payload,
                    idempotency_key=str(uuid.uuid4()),
                    on_event=on_event,
                    settings=_live_settings(app.state.settings, app.state.orchestrator.extractor),
                )

            route_settings = app.state.settings
            if not isinstance(app.state.orchestrator.extractor, (GroqExtractor, GeminiExtractor)):
                route_settings = replace(route_settings, extractor="template")
            outcome = execute_chat(
                message=message,
                has_file=bool(uploads),
                prior=prior,
                settings=route_settings,
                run_invoice=run_invoice,
                workbook_path=app.state.workbook_path,
                uploads=uploads,
                progress=progress_lines(app.state.store.list_tasks()),
                on_event=on_event,
            )
            if outcome.get("reply"):
                events.put(("say", {"role": "agent", "text": outcome["reply"]}))
            events.put(("done", outcome))
        except Exception as exc:
            events.put(("error", {"detail": str(exc)}))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()
    while True:
        item = events.get()
        if item is None:
            break
        kind, body = item
        yield f"event: {kind}\ndata: {json.dumps(body)}\n\n"


def _live_settings(settings: Settings, extractor: object) -> Settings | None:
    if isinstance(extractor, (GroqExtractor, GeminiExtractor)) and settings.groq_api_key and settings.extractor != "template":
        return settings
    return None


def _extractor(settings: Settings) -> object:
    if settings.extractor == "template":
        return TemplateExtractor()
    if not settings.groq_api_key:
        return _MissingKeyExtractor()
    return GroqExtractor(settings.groq_api_key, settings.model)


class _MissingKeyExtractor:
    def extract(self, document_text: str, filename: str) -> object:
        del document_text, filename
        raise ExtractionError("GROQ_API_KEY is not set. This prototype does not call another model.")


def _task_events(
    orchestrator: Orchestrator,
    instruction: str,
    filename: str,
    payload: bytes,
    key: str,
) -> Iterator[str]:
    events: queue.Queue[tuple[str, dict] | None] = queue.Queue()

    def on_event(event: dict) -> None:
        events.put(("audit", event))

    def work() -> None:
        try:
            result = orchestrator.run(
                instruction=instruction,
                filename=filename,
                pdf_bytes=payload,
                idempotency_key=key,
                on_event=on_event,
            )
            events.put(("done", result))
        except Exception as exc:
            events.put(("error", {"detail": str(exc)}))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()
    while True:
        item = events.get()
        if item is None:
            break
        kind, body = item
        yield f"event: {kind}\ndata: {json.dumps(body)}\n\n"


def _review(store: AccountingStore, invoice_id: int, outcome: str, action) -> dict:
    try:
        saved = action(invoice_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Invoice not found.") from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if saved["task_id"]:
        store.add_audit(saved["task_id"], "human_review", outcome, {"invoice_id": invoice_id})
    return _public_invoice(saved)


def _public_invoice(row: dict) -> dict:
    payload = {key: value for key, value in row.items() if key not in HIDDEN_CENTS}
    payload["source"] = "legacy"
    return payload


def _task_for_sheet_invoice(store: AccountingStore, invoice_id: str) -> dict | None:
    needle = invoice_id.casefold()
    found = None
    for task in store.list_tasks():
        if needle in str(task.get("filename") or "").casefold():
            found = task
    return found


def _install_frontend(app: FastAPI) -> None:
    assets = DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        page = DIST / "index.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="The dashboard has not been built.")
        return FileResponse(page, headers={"Cache-Control": "no-cache"})


app = create_app()
