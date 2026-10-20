"""Run a chat turn: route the message, then act without asking permission."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.agent_router import plan_actions
from app.config import Settings
from app.llm import generate_json
from app.outbound import HttpClient, UrlClient, deliver, invoice_label, persisted_ok, result_text

_SEND_TOOLS = {"send_email", "post_slack"}


def execute_chat(
    *,
    message: str,
    has_file: bool,
    prior: dict | None,
    settings: Settings,
    run_invoice,
    workbook_path: Path | None = None,
    client: HttpClient | None = None,
    uploads: list[tuple[str, bytes]] | None = None,
    progress: list[str] | None = None,
    on_event=None,
) -> dict:
    has_result = _usable(prior)
    steps = plan_actions(message, has_file=has_file, has_result=has_result, settings=settings)
    talk_route = not has_file and _should_talk(steps)
    if talk_route:
        return {"task": prior, "reply": talk(message, settings, progress or _progress_from_task(prior))}
    sender = client or UrlClient()
    result = prior
    lines: list[str] = []
    batch = uploads or [None]
    for index, step in enumerate(steps):
        tool = step["tool"]
        if tool == "ask":
            lines.append(step["question"])
            break
        if tool == "reply":
            lines.append(talk(message, settings, progress or _progress_from_task(prior)))
            break
        if tool == "process_invoice":
            for item in batch:
                result = run_invoice(item[0], item[1]) if item else run_invoice()
                if result.get("status") == "waiting_for_user":
                    _remember(workbook_path, result.get("id"), steps[index + 1 :])
                    lines.append(result.get("explanation") or "I need one detail before I continue.")
                    return {"task": result, "reply": " ".join(line for line in lines if line).strip()}
                label = item[0] if item else "Invoice"
                lines.append(f"{label}: {narrate_run(result, settings)}")
            continue
        if tool in _SEND_TOOLS:
            if not result or result.get("status") in {None, "", "running", "waiting_for_user"}:
                lines.append("I need to finish the invoice before I send that.")
                break
            if not persisted_ok(result):
                lines.append(result_text(result))
                break
            lines.append(_shown_delivery(step, result, settings, sender, on_event))
    return {"task": result, "reply": " ".join(line for line in lines if line).strip()}


def resume_pending(
    *,
    task: dict,
    settings: Settings,
    workbook_path: Path,
    client: HttpClient | None = None,
) -> dict:
    if task.get("status") == "waiting_for_user":
        return {"task": task, "reply": task.get("explanation") or ""}
    steps = _take_pending(workbook_path, task.get("id"))
    if not steps:
        return {"task": task, "reply": task.get("explanation") or ""}
    sender = client or UrlClient()
    lines = [str(task.get("explanation") or "").strip()]
    for step in steps:
        if step["tool"] == "ask":
            lines.append(step["question"])
            break
        if step["tool"] in _SEND_TOOLS:
            if not persisted_ok(task):
                lines.append(result_text(task))
                break
            lines.append(_shown_delivery(step, task, settings, sender, None))
    return {"task": task, "reply": " ".join(line for line in lines if line).strip()}


def _usable(task: dict | None) -> bool:
    if not task:
        return False
    return task.get("status") not in {None, "", "running"}


def talk(message: str, settings: Settings, progress: list[str]) -> str:
    if settings.extractor != "template" and settings.groq_api_key:
        try:
            reply = generate_json(settings.groq_api_key, settings.model, _talk_prompt(message, settings, progress), max_tokens=700)
            text = str(reply.payload.get("reply") or "").strip()
            if text:
                return text
        except Exception:
            pass
    return _local_briefing(progress, settings)


def _should_talk(steps: list[dict]) -> bool:
    """True only for pure conversation turns — never swallow ask/process/send steps."""
    tools = [step["tool"] for step in steps]
    if not tools:
        return True
    return tools == ["reply"]


def _talk_prompt(message: str, settings: Settings, progress: list[str]) -> str:
    access = "\n".join(f"- {line}" for line in _access_lines(settings))
    state = "\n".join(f"- {line}" for line in progress) if progress else "- Nothing is in progress."
    return (
        "You are the ledger computer-use agent. The user sent a chat with no file.\n"
        "Start the reply with a short greeting. Then say what you can do, with enough detail to be useful and not a long essay.\n"
        "Cover the SQL ledger, checking the vendor, purchase order, and goods receipt, and AgentMail and Slack after a verified row.\n"
        "Then say what you can access and the current progress, and answer their message in plain language.\n"
        "Do not ask for permission. Do not invent an invoice total or a decision.\n"
        "Do not mention Google Sheets or Notion.\n"
        "What you can do:\n"
        "- Record one or more invoices on the SQL ledger. You check the vendor, purchase order, and goods receipt, then fill one row.\n"
        "- Pause only when several vendors match and a person must pick one.\n"
        "- After a verified row, email that result with AgentMail and post it in Slack.\n"
        f"Access:\n{access}\n"
        f"Current progress:\n{state}\n"
        f"User: {message}\n"
        'Reply JSON: {"reply":"..."}\n'
    )


def _local_briefing(progress: list[str], settings: Settings) -> str:
    access = " ".join(_access_lines(settings))
    state = " ".join(progress) if progress else "Nothing is in progress."
    return (
        "Hello. I record invoices on the SQL ledger. "
        "I check the vendor, the purchase order, and the goods receipt, then fill one row on the ledger grid. "
        "After a verified row, I can email that result with AgentMail and post it in Slack. "
        f"I can access {access} "
        f"Current progress: {state}"
    )


def _access_lines(settings: Settings) -> list[str]:
    lines = [
        "the ledger tabs Invoice_Ledger, Purchase_Orders, Goods_Receipts, and Vendors.",
        "the invoice files you attach.",
        "the task history for this ledger.",
    ]
    lines.append("AgentMail." if settings.agentmail_api_key and settings.agentmail_inbox_id else "AgentMail is not connected.")
    lines.append("Slack, including the general channel." if settings.slack_bot_token else "Slack is not connected.")
    lines.append("I do not use a shell, or mark a row POSTED.")
    return lines


def progress_lines(tasks: list[dict]) -> list[str]:
    lines: list[str] = []
    for task in list(tasks)[-5:][::-1]:
        lines.extend(_progress_from_task(task))
    return lines


def narrate_run(task: dict, settings: Settings) -> str:
    explanation = _plain_error(str(task.get("explanation") or "").strip())
    facts = explanation or "I finished this invoice."
    if settings.extractor == "template" or not settings.groq_api_key:
        return facts
    goals = []
    for event in (task.get("audit") or [])[-4:]:
        if event.get("step") in {"cua_act", "cua_verify"}:
            goal = str((event.get("detail") or {}).get("goal") or "").strip()
            if goal:
                goals.append(goal)
    trail = "\n".join(f"- {goal}" for goal in goals) or "- The ledger actions are stored on the task."
    prompt = (
        "You are the ledger computer-use agent. State what this run did in one or two factual sentences.\n"
        "Use only the facts below. Do not greet the user. Do not recap every action.\n"
        "Do not invent a total, a status, or a decision.\n"
        "Do not mention Google Sheets or Notion.\n"
        f"Invoice: {task.get('filename') or 'invoice'}\n"
        f"Status: {task.get('status')}\n"
        f"Decision: {task.get('decision')}\n"
        f"Verified: {task.get('verified')}\n"
        f"Explanation: {facts}\n"
        f"Actions:\n{trail}\n"
        'Reply JSON: {"reply":"..."}\n'
    )
    try:
        reply = generate_json(settings.groq_api_key, settings.model, prompt, max_tokens=220)
        text = str(reply.payload.get("reply") or "").strip()
        if text:
            return text
    except Exception:
        pass
    return facts


def _shown_delivery(step: dict, task: dict, settings: Settings, sender: HttpClient, on_event) -> str:
    body = result_text(task, str(step.get("note") or ""))
    label = invoice_label(task)
    subject = f"Invoice {label} {task.get('decision') or 'undecided'}"
    if step["tool"] == "send_email":
        _emit(on_event, "open_mail", {"to": step.get("to") or "", "subject": subject})
        _emit(on_event, "type_mail", {"to": step.get("to") or "", "subject": subject, "body": body})
        text = deliver(step, task, settings, sender)
        if text.startswith("I emailed"):
            _emit(on_event, "send_mail", {"to": step.get("to") or "", "subject": subject, "body": body})
        return text
    if step["tool"] == "post_slack":
        channel = step.get("channel") or "general"
        _emit(on_event, "open_slack", {"channel": channel})
        _emit(on_event, "type_slack", {"channel": channel, "body": body})
        text = deliver(step, task, settings, sender)
        if text.startswith("I posted"):
            _emit(on_event, "post_slack", {"channel": channel, "body": body})
        return text
    return deliver(step, task, settings, sender)


def _emit(on_event, step: str, detail: dict) -> None:
    if on_event is None:
        return
    on_event(
        {
            "step": step,
            "outcome": "ok",
            "detail": detail,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _plain_error(text: str) -> str:
    lowered = text.casefold()
    if "<!doctype" in lowered or "<html" in lowered or "ppconfig" in lowered:
        return "An earlier attempt could not reach the ledger."
    return text


def _progress_from_task(task: dict | None) -> list[str]:
    if not task:
        return []
    filename = task.get("filename") or "an invoice"
    status = task.get("status") or "unknown"
    decision = task.get("decision") or "no decision yet"
    explanation = _plain_error(str(task.get("explanation") or "").strip())
    line = f"{filename} is {status} ({decision})."
    if explanation:
        line = f"{line} {explanation}"
    return [line]


def _remember(workbook_path: Path | None, task_id: object, steps: list[dict]) -> None:
    if workbook_path is None or not task_id or not steps:
        return
    kept = [step for step in steps if step["tool"] != "reply"]
    if not kept:
        return
    path = _pending_path(workbook_path, str(task_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept), encoding="utf-8")


def _take_pending(workbook_path: Path, task_id: object) -> list[dict]:
    if not task_id:
        return []
    path = _pending_path(workbook_path, str(task_id))
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raw = []
    path.unlink(missing_ok=True)
    if not isinstance(raw, list):
        return []
    return [step for step in raw if isinstance(step, dict) and step.get("tool")]


def _pending_path(workbook_path: Path, task_id: str) -> Path:
    return workbook_path.parent / "cua_sessions" / f"{task_id}.pending.json"
