"""Choose the next tools from a chat message. Python still blocks a missing detail."""

from __future__ import annotations

import re

from app.config import Settings
from app.llm import generate_json

TOOLS = (
    "process_invoice",
    "send_email",
    "post_slack",
    "ask",
    "reply",
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CHANNEL = re.compile(r"#([A-Za-z0-9][A-Za-z0-9_\-]{0,79})")
_LEDGER_MAIL = re.compile(r"\b(?:e-mail|email|mail)\b(?:\s+(?:me|us|the|this|our|my)){0,3}\s+ledger\b")
_INVOICE_HINTS = ("invoice", "workbook", "ledger", "google sheet", "process", "handle", "look after", "look at", "record this")
_MAIL_HINTS = ("email", "e-mail", "mail")


def plan_actions(message: str, *, has_file: bool, has_result: bool, settings: Settings) -> list[dict]:
    if _wants_current_ledger_mail(message):
        recipient = _first_email(message)
        if not recipient:
            return [_step("ask", question="Who should I email?")]
        return [_step("send_ledger", to=recipient)]
    if settings.extractor != "template" and settings.groq_api_key:
        try:
            planned = _from_model(message, has_file=has_file, has_result=has_result, settings=settings)
        except Exception:
            planned = _from_keywords(message, has_file=has_file, has_result=has_result, settings=settings)
    else:
        planned = _from_keywords(message, has_file=has_file, has_result=has_result, settings=settings)
    return _guard(planned, message, has_file=has_file, has_result=has_result, settings=settings)


def _from_model(message: str, *, has_file: bool, has_result: bool, settings: Settings) -> list[dict]:
    channel = settings.slack_default_channel or "general"
    prompt = (
        "You are the ledger agent. Do not ask for permission. Pick the tools to run, in order.\n"
        "Tools: process_invoice, send_email, post_slack, ask, reply.\n"
        "Use ask only when a required detail is missing: the invoice file, or the email recipient.\n"
        "If the user asked to mail the current ledger, do not ask for an invoice file or a ledger folder.\n"
        "Slack posts go to the general channel unless the user names a #channel.\n"
        "Use reply when the user is only asking about a finished run.\n"
        f"has_file: {str(has_file).lower()}\n"
        f"has_result: {str(has_result).lower()}\n"
        f"default_slack_channel: {channel}\n"
        f"User: {message}\n"
        'Reply JSON: {"steps":[{"tool":"reply","to":"","channel":"","note":"","question":""}]}\n'
    )
    reply = generate_json(settings.groq_api_key, settings.model, prompt, max_tokens=600)
    raw = reply.payload.get("steps")
    if not isinstance(raw, list):
        return [{"tool": "reply", "to": "", "channel": "", "note": "", "question": ""}]
    steps = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "")
        if tool not in TOOLS:
            continue
        steps.append(_step(tool, item.get("to"), item.get("channel"), item.get("note"), item.get("question")))
    return steps or [{"tool": "reply", "to": "", "channel": "", "note": "", "question": ""}]


def _from_keywords(message: str, *, has_file: bool, has_result: bool, settings: Settings) -> list[dict]:
    text = message.casefold()
    wants_invoice = any(hint in text for hint in _INVOICE_HINTS)
    wants_mail = any(hint in text for hint in _MAIL_HINTS)
    wants_slack = "slack" in text
    if not any((wants_invoice, wants_mail, wants_slack)):
        return [_step("reply")]
    steps: list[dict] = []
    if wants_invoice and (has_file or not has_result):
        steps.append(_step("process_invoice"))
    if wants_mail:
        steps.append(_step("send_email", to=_first_email(message)))
    if wants_slack:
        steps.append(_step("post_slack", channel=_channel(message, settings)))
    return steps


def _guard(steps: list[dict], message: str, *, has_file: bool, has_result: bool, settings: Settings) -> list[dict]:
    planned = list(steps)
    if has_file and not any(step["tool"] == "process_invoice" for step in planned):
        planned = [_step("process_invoice"), *planned]
    guarded: list[dict] = []
    for step in planned:
        tool = step["tool"]
        if tool == "reply" and has_file:
            continue
        if tool == "process_invoice":
            if not has_file and not has_result:
                return [_step("ask", question="Attach the invoice.")]
            if not has_file:
                continue
            guarded.append(step)
            continue
        if tool == "send_email":
            recipient = step.get("to") or _first_email(message)
            if not recipient:
                guarded.append(_step("ask", question="Who should I email?"))
                break
            guarded.append(_step("send_email", to=recipient, note=step.get("note") or ""))
            continue
        if tool == "post_slack":
            channel = step.get("channel") or _channel(message, settings)
            if not channel:
                guarded.append(_step("ask", question="Which Slack channel?"))
                break
            guarded.append(_step("post_slack", channel=channel, note=step.get("note") or ""))
            continue
        if tool == "ask":
            if has_file and not _user_needs_send_detail(message, settings):
                continue
            question = str(step.get("question") or "").strip() or "What detail should I use?"
            guarded.append(_step("ask", question=question))
            break
        guarded.append(step)
        if tool == "reply":
            break
    if not guarded:
        return [_step("reply")]
    if guarded[0]["tool"] == "ask" and len(guarded) == 1:
        return guarded
    return guarded


def _step(
    tool: str,
    to: object = "",
    channel: object = "",
    note: object = "",
    question: object = "",
) -> dict:
    return {
        "tool": tool,
        "to": str(to or "").strip(),
        "channel": str(channel or "").strip().lstrip("#"),
        "note": str(note or "").strip()[:280],
        "question": str(question or "").strip(),
    }


def _wants_current_ledger_mail(message: str) -> bool:
    text = message.casefold()
    mentions_mail = any(hint in text for hint in _MAIL_HINTS) or (
        "send" in text and ("ledger folder" in text or "current ledger" in text)
    )
    if not mentions_mail:
        return False
    if "ledger folder" in text or "current ledger" in text:
        return True
    return _LEDGER_MAIL.search(text) is not None


def _user_needs_send_detail(message: str, settings: Settings) -> bool:
    text = message.casefold()
    if any(hint in text for hint in _MAIL_HINTS) and not _first_email(message):
        return True
    if "slack" in text and not _channel(message, settings):
        return True
    return False


def _first_email(message: str) -> str:
    found = _EMAIL.search(message)
    return found.group(0) if found else ""


def _channel(message: str, settings: Settings) -> str:
    found = _CHANNEL.search(message)
    if found:
        return found.group(1)
    named = settings.slack_default_channel.lstrip("#")
    return named or "general"
