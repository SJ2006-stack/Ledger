from fastapi.testclient import TestClient

from app.cua.memory_browser import MemoryBrowser
from app.cua.workbook_io import reset_workbook
from app.documents import CLEAN, invoice_pdf
from app.extractor import TemplateExtractor
from app.main import create_app


def test_stream_shows_each_real_step(tmp_path):
    client = TestClient(create_app(tmp_path / "stream.db", TemplateExtractor()))
    with client.stream(
        "POST",
        "/api/tasks/stream",
        data={"instruction": "Post this invoice if it matches.", "idempotency_key": "stream-1"},
        files={"file": (CLEAN.filename, invoice_pdf(CLEAN), "application/pdf")},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: audit" in body
    assert "parse_document" in body
    assert "verify_persisted" in body
    assert "event: done" in body
    assert '"status": "completed"' in body


def test_task_and_accounting_api(tmp_path):
    client = TestClient(create_app(tmp_path / "api.db", TemplateExtractor()))
    response = client.post(
        "/api/tasks",
        data={
            "instruction": "Post this invoice if it matches the purchase order and goods receipt.",
            "idempotency_key": "api-clean",
        },
        files={"file": (CLEAN.filename, invoice_pdf(CLEAN), "application/pdf")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["verified"] is True
    invoices = client.get("/api/invoices").json()
    posted = next(row for row in invoices if row["invoice_number"] == "INV-2001")
    assert posted["status"] == "pending_approval"
    assert posted["total"] == "5900.00"
    detail = client.get(f"/api/invoices/{posted['id']}").json()
    assert detail["task"]["status"] == "completed"
    approved = client.post(f"/api/invoices/{posted['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    created = client.post(
        "/api/accounting/invoices",
        json={
            "vendor_id": 1,
            "vendor_name": "Northwind Supplies",
            "invoice_number": "INV-API-1",
            "po_number": "PO-1001",
            "currency": "INR",
            "subtotal": "10.00",
            "tax_rate_percent": "18",
            "tax_amount": "1.80",
            "total": "11.80",
            "status": "needs_review",
            "idempotency_key": "api-held",
            "lines": [
                {"sku": "SKU-WA", "description": "Widget A", "quantity": "1", "unit_price": "10.00"}
            ],
        },
    )
    assert created.status_code == 200
    assert created.json()["created"] is True
    replay = client.post(
        "/api/accounting/invoices",
        json={
            "vendor_id": 1,
            "vendor_name": "Northwind Supplies",
            "invoice_number": "INV-API-1",
            "po_number": "PO-1001",
            "currency": "INR",
            "subtotal": "10.00",
            "tax_rate_percent": "18",
            "tax_amount": "1.80",
            "total": "11.80",
            "status": "needs_review",
            "idempotency_key": "api-held",
            "lines": [
                {"sku": "SKU-WA", "description": "Widget A", "quantity": "1", "unit_price": "10.00"}
            ],
        },
    )
    assert replay.json()["created"] is False
    assert replay.json()["invoice"]["id"] == created.json()["invoice"]["id"]
    blocked = client.post(f"/api/invoices/{created.json()['invoice']['id']}/approve")
    assert blocked.status_code == 409


def test_ask_returns_the_thought_and_the_answer(monkeypatch, tmp_path):
    def fake(settings, payload):
        del settings
        assert payload["question"] == "Why was this held?"
        assert "tax did not match" in payload["steps"][0]["line"]
        return {"answer": "The tax did not match.", "thinking": "I compared the tax line.", "thoughts_tokens": 12}

    monkeypatch.setattr("app.main.ask_about_run", fake)
    client = TestClient(create_app(tmp_path / "ask.db", TemplateExtractor()))
    response = client.post(
        "/api/ask",
        json={
            "question": "Why was this held?",
            "instruction": "Post only if the totals agree.",
            "steps": [{"step": "decide", "outcome": "needs_review", "line": "The tax did not match.", "thinking": ""}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "The tax did not match."
    assert body["thinking"] == "I compared the tax line."


def test_ask_requires_a_question(tmp_path):
    client = TestClient(create_app(tmp_path / "ask-empty.db", TemplateExtractor()))
    response = client.post("/api/ask", json={"question": "  "})
    assert response.status_code == 400


def test_health_reports_visible_cua_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CUA_HEADLESS", raising=False)
    client = TestClient(create_app(tmp_path / "health.db", TemplateExtractor()))
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["cua_headless"] is False
    cfg = client.get("/api/cua/config").json()
    assert cfg["headless"] is False


def test_cua_stream_runs_with_memory_browser(tmp_path):
    books = tmp_path / "Finance_AP_Ledger.xlsx"
    reset_workbook(books)
    app = create_app(tmp_path / "cua-stream.db", TemplateExtractor(), workbook_path=books)
    browser = MemoryBrowser(books)
    app.state.cua_browser_factory = lambda task_id: browser
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/tasks/cua/stream",
        data={"instruction": "Record this invoice in the workbook.", "idempotency_key": "cua-stream-1"},
        files={"file": (CLEAN.filename, invoice_pdf(CLEAN), "application/pdf")},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: audit" in body
    assert "cua_act" in body or "match_records" in body
    assert "event: done" in body
    assert '"status": "completed"' in body
    assert '"verified": true' in body


def test_cua_headless_env_is_honored_in_health(tmp_path, monkeypatch):
    monkeypatch.setenv("CUA_HEADLESS", "1")
    client = TestClient(create_app(tmp_path / "headless.db", TemplateExtractor()))
    assert client.get("/api/health").json()["cua_headless"] is True
    monkeypatch.setenv("CUA_HEADLESS", "0")
