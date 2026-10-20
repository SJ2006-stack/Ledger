from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_router import plan_actions
from app.chat import _talk_prompt, execute_chat, resume_pending
from app.config import Settings
from app.cua.memory_browser import MemoryBrowser
from app.documents import CLEAN, invoice_pdf
from app.extractor import TemplateExtractor
from app.main import create_app
from app.outbound import deliver


def _settings(**overrides) -> Settings:
    values = {
        "groq_api_key": "",
        "model": "template",
        "extractor": "template",
        "db_path": Path("unused"),
    }
    values.update(overrides)
    return Settings(**values)


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url: str, *, headers: dict, payload: dict) -> dict:
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        return {"ok": True, "message_id": "m1"}


def test_attached_invoice_selects_the_workbook_without_asking():
    steps = plan_actions("Look after this invoice", has_file=True, has_result=False, settings=_settings())
    assert steps[0]["tool"] == "process_invoice"
    assert all(step["tool"] != "ask" for step in steps)


def test_missing_file_asks_and_does_not_call_a_tool():
    ran = []
    outcome = execute_chat(
        message="Look after the invoice",
        has_file=False,
        prior=None,
        settings=_settings(),
        run_invoice=lambda: ran.append("run"),
    )
    assert ran == []
    assert outcome["reply"] == "Attach the invoice."
    assert outcome["task"] is None


def test_email_without_an_address_asks_and_does_not_send():
    client = _FakeHttp()
    outcome = execute_chat(
        message="Email the vendor",
        has_file=False,
        prior={"id": "t1", "status": "verified", "decision": "ready", "explanation": "Ready.", "filename": "a.pdf"},
        settings=_settings(agentmail_api_key="key", agentmail_inbox_id="inbox"),
        run_invoice=lambda: None,
        client=client,
    )
    assert client.calls == []
    assert outcome["reply"] == "Who should I email?"


def test_failed_verification_does_not_send_a_success_claim():
    client = _FakeHttp()
    outcome = execute_chat(
        message="Email the result to ap@example.com",
        has_file=False,
        prior={
            "id": "t-failed",
            "status": "failed",
            "verified": False,
            "decision": "needs_review",
            "explanation": "Independent Google Sheet verification did not pass.",
            "filename": "northwind-inv-2001.pdf",
            "issues": ["TAX_MISMATCH"],
        },
        settings=_settings(agentmail_api_key="key", agentmail_inbox_id="inbox"),
        run_invoice=lambda filename, payload: {},
        client=client,
    )
    assert client.calls == []
    assert "could not be verified" in outcome["reply"]
    assert "updated and verified" not in outcome["reply"]
    assert "INV-2001" in outcome["reply"]
    assert "TAX_MISMATCH" in outcome["reply"]


def test_outbound_body_uses_the_decision_not_a_model_total():
    client = _FakeHttp()
    task = {
        "filename": "northwind-inv-2001.pdf",
        "decision": "ready",
        "explanation": "The quantities match.",
    }
    line = deliver(
        {"tool": "send_email", "to": "ap@example.com", "note": "The total is 1.00"},
        task,
        _settings(agentmail_api_key="key", agentmail_inbox_id="inbox"),
        client,
    )
    assert line == "I emailed ap@example.com."
    body = client.calls[0]["payload"]["text"]
    assert "ready" in body
    assert "1.00" not in body
    assert client.calls[0]["url"] == "https://api.agentmail.to/v0/inboxes/inbox/messages/send"


def test_mail_waits_until_the_vendor_is_chosen(tmp_path):
    client = _FakeHttp()
    books = tmp_path / "book.xlsx"
    outcome = execute_chat(
        message="Look after this invoice and email ap@example.com",
        has_file=True,
        prior=None,
        settings=_settings(agentmail_api_key="key", agentmail_inbox_id="inbox"),
        run_invoice=lambda: {
            "id": "task-1",
            "status": "waiting_for_user",
            "decision": None,
            "explanation": "Several vendors match.",
            "filename": "helios-inv-5005.pdf",
        },
        workbook_path=books,
        client=client,
    )
    assert client.calls == []
    assert outcome["task"]["status"] == "waiting_for_user"
    finished = {
        "id": "task-1",
        "status": "verified",
        "decision": "ready",
        "explanation": "The quantities match.",
        "filename": "helios-inv-5005.pdf",
    }
    resumed = resume_pending(task=finished, settings=_settings(agentmail_api_key="key", agentmail_inbox_id="inbox"), workbook_path=books, client=client)
    assert "ready" in client.calls[0]["payload"]["text"]
    assert "1.00" not in client.calls[0]["payload"]["text"]
    assert "I emailed ap@example.com." in resumed["reply"]


def test_several_attached_files_each_run_on_the_workbook():
    seen: list[str] = []

    def run_invoice(filename, payload):
        seen.append(filename)
        assert payload
        return {
            "id": filename,
            "status": "verified",
            "decision": "ready",
            "explanation": "The quantities match.",
            "filename": filename,
        }

    outcome = execute_chat(
        message="Look after these invoices",
        has_file=True,
        prior=None,
        settings=_settings(),
        run_invoice=run_invoice,
        uploads=[("northwind-inv-2001.pdf", b"a"), ("harbor-inv-2002.pdf", b"b")],
    )
    assert seen == ["northwind-inv-2001.pdf", "harbor-inv-2002.pdf"]
    assert "northwind-inv-2001.pdf" in outcome["reply"]
    assert "harbor-inv-2002.pdf" in outcome["reply"]


def test_current_ledger_email_uses_sql_rows_and_not_groq(monkeypatch):
    def boom(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Groq should not write the ledger email")

    monkeypatch.setattr("app.agent_router.generate_json", boom)
    monkeypatch.setattr("app.chat.generate_json", boom)
    client = _FakeHttp()
    rows = [
        {
            "Invoice ID": "INV-3003",
            "Vendor": "Apex Industrial",
            "Tax ID": "07AADCA1111B1Z8",
            "Invoice Date": "2026-09-15",
            "PO ID": "PO-3003",
            "GRN ID": "GRN-3003",
            "Subtotal": "2000.00",
            "Tax Rate": "18",
            "Tax Amount": "360.00",
            "Total": "2360.00",
            "Status": "NEEDS_APPROVAL",
            "Exception": "",
            "Source PDF": "apex-inv-3003.pdf",
            "Processed At": "seed",
        }
    ]
    ran: list[str] = []
    outcome = execute_chat(
        message="mail the current ledger folder on my email shriyanshjaiswl.xf@gmail.com",
        has_file=False,
        prior=None,
        settings=_settings(groq_api_key="k", extractor="groq", agentmail_api_key="key", agentmail_inbox_id="inbox"),
        run_invoice=lambda *args: ran.append("run"),
        client=client,
        ledger_rows=rows,
    )
    assert ran == []
    assert outcome["reply"] == "I emailed shriyanshjaiswl.xf@gmail.com."
    assert "invoice file" not in outcome["reply"].casefold()
    payload = client.calls[0]["payload"]
    assert set(payload) == {"to", "subject", "text"}
    assert payload["to"] == "shriyanshjaiswl.xf@gmail.com"
    assert payload["subject"] == "Invoice Ledger"
    assert payload["text"].splitlines()[0] == "\t".join(
        [
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
        ]
    )
    assert "INV-3003" in payload["text"]
    assert "Apex Industrial" in payload["text"]
    assert "2360.00" in payload["text"]
    assert ".env" not in payload["text"]
    assert "GROQ" not in payload["text"]
    assert client.calls[0]["url"] == "https://api.agentmail.to/v0/inboxes/inbox/messages/send"


def test_ledger_mail_without_an_address_does_not_send():
    client = _FakeHttp()
    outcome = execute_chat(
        message="mail the current ledger folder",
        has_file=False,
        prior=None,
        settings=_settings(agentmail_api_key="key", agentmail_inbox_id="inbox"),
        run_invoice=lambda: None,
        client=client,
    )
    assert client.calls == []
    assert outcome["reply"] == "Who should I email?"


def test_talk_prompt_does_not_ask_for_a_ledger_upload():
    prompt = _talk_prompt(
        "mail the current ledger folder on my email shriyanshjaiswl.xf@gmail.com",
        _settings(),
        [],
    )
    assert "do not tell them to upload a ledger folder" in prompt.casefold()


def test_chat_stream_asks_for_the_file_and_can_run_the_workbook(tmp_path):
    books = tmp_path / "Finance_AP_Ledger.xlsx"
    app = create_app(tmp_path / "api.db", TemplateExtractor(), workbook_path=books)
    app.state.cua_browser_factory = lambda task_id: MemoryBrowser(books)
    client = TestClient(app)
    missing = client.post("/api/chat/stream", data={"message": "What can you do?"})
    assert missing.status_code == 200
    assert "Hello." in missing.text
    assert "SQL ledger" in missing.text
    assert "purchase order" in missing.text
    assert "goods receipt" in missing.text
    assert "AgentMail" in missing.text
    assert "Slack" in missing.text
    assert "ledger grid" in missing.text.casefold()
    assert "Current progress:" in missing.text
    assert "Google Sheet" not in missing.text
    assert "Notion" not in missing.text
    assert "event: audit" not in missing.text

    processed = client.post(
        "/api/chat/stream",
        data={"message": "Look after this invoice"},
        files={"file": ("northwind-inv-2001.pdf", invoice_pdf(CLEAN), "application/pdf")},
    )
    assert processed.status_code == 200
    assert "Who should I" not in processed.text
    assert "ready" in processed.text
