"""Send the invoice result. The body is built from the rules, not from the model."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from urllib.parse import quote
from typing import Protocol

from app.config import Settings

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_INVOICE = re.compile(r"(inv-\d+)", re.IGNORECASE)


class HttpClient(Protocol):
    def post(self, url: str, *, headers: dict, payload: dict) -> dict: ...


class UrlClient:
    def post(self, url: str, *, headers: dict, payload: dict) -> dict:
        return _request(url, method="POST", headers=headers, payload=payload)


def invoice_label(task: dict) -> str:
    filename = str(task.get("filename") or "")
    found = _INVOICE.search(filename)
    if found:
        return found.group(1).upper()
    return filename or "invoice"


def persisted_ok(task: dict | None) -> bool:
    if not task:
        return False
    status = str(task.get("status") or "")
    if status in {"", "running", "failed", "needs_recovery", "waiting_for_user"}:
        return False
    if task.get("verified") is False:
        return False
    return bool(task.get("verified")) or status in {"verified", "completed", "needs_review", "pending_approval"}


def result_text(task: dict, note: str = "") -> str:
    decision = str(task.get("decision") or "undecided")
    explanation = str(task.get("explanation") or "").strip()
    label = invoice_label(task)
    issues = task.get("issues") or []
    exception = ", ".join(str(issue) for issue in issues if issue)
    if not persisted_ok(task):
        lead = "Ledger update could not be verified."
    elif decision == "ready":
        lead = "Ledger updated and verified. Invoice pending human approval."
    elif decision in {"needs_review", "missing_information"}:
        lead = "Invoice held for review."
    else:
        lead = "Ledger update could not be verified."
    base = f"{lead} Invoice {label}: {decision}. Status {task.get('status') or 'undecided'}."
    if exception:
        base = f"{base} Exception: {exception}."
    if explanation:
        base = f"{base} {explanation}"
    extra = note.strip()
    if extra and _note_ok(extra, base):
        return f"{base}\n{extra}"
    return base


def deliver(step: dict, task: dict, settings: Settings, client: HttpClient) -> str:
    tool = step["tool"]
    body = result_text(task, str(step.get("note") or ""))
    label = invoice_label(task)
    decision = str(task.get("decision") or "undecided")
    subject = f"Invoice {label} {decision}"
    try:
        if tool == "send_email":
            return _email(step, settings, client, subject, body)
        if tool == "post_slack":
            return _slack(step, settings, client, body)
    except Exception as exc:
        return f"{_service_name(tool)} could not be reached. {exc}"[:240]
    return "I did not recognize that tool."


def _email(step: dict, settings: Settings, client: HttpClient, subject: str, body: str) -> str:
    if not settings.agentmail_api_key or not settings.agentmail_inbox_id:
        return "AgentMail is not connected."
    inbox = quote(settings.agentmail_inbox_id, safe="")
    client.post(
        f"https://api.agentmail.to/v0/inboxes/{inbox}/messages/send",
        headers=_bearer(settings.agentmail_api_key),
        payload={"to": step["to"], "subject": subject, "text": body},
    )
    return f"I emailed {step['to']}."


def _slack(step: dict, settings: Settings, client: HttpClient, body: str) -> str:
    if not settings.slack_bot_token:
        return "Slack is not connected."
    result = client.post(
        "https://slack.com/api/chat.postMessage",
        headers=_bearer(settings.slack_bot_token),
        payload={"channel": step["channel"], "text": body},
    )
    if result.get("ok") is False:
        return f"Slack did not post. {result.get('error') or 'unknown error'}"
    return f"I posted to Slack #{step['channel']}."


def _request(url: str, *, method: str, headers: dict, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:240]
        raise RuntimeError(detail or f"HTTP {exc.code}") from exc
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[:240]}
    return loaded if isinstance(loaded, dict) else {"raw": raw[:240]}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _service_name(tool: str) -> str:
    names = {
        "send_email": "AgentMail",
        "post_slack": "Slack",
    }
    return names.get(tool, "The service")


def _note_ok(note: str, base: str) -> bool:
    for number in _NUMBER.findall(note):
        if number not in base:
            return False
    return True
