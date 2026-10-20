import json

from app.config import Settings
from app.extractor import ExtractionError
from app.llm import generate_json

ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "thinking": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["thinking", "answer"],
}


def ask_about_run(settings: Settings, payload: dict) -> dict:
    if not settings.groq_api_key:
        raise ExtractionError("GROQ_API_KEY is not set. This prototype does not call another model.")
    question = str(payload.get("question") or "").strip()
    prompt = _prompt(question, _context(payload))
    try:
        reply = generate_json(
            settings.groq_api_key,
            settings.model,
            prompt,
            max_tokens=2048,
        )
    except Exception as exc:
        raise ExtractionError(f"Model request failed: {exc}") from exc
    return {
        "answer": str(reply.payload.get("answer") or "").strip(),
        "thinking": str(reply.payload.get("thinking") or "").strip(),
        "thoughts_tokens": 0,
    }


def _context(payload: dict) -> str:
    lines = [
        f"Instruction: {str(payload.get('instruction') or '').strip()}",
        f"File: {str(payload.get('filename') or '').strip()}",
        f"Result: {str(payload.get('explanation') or '').strip()}",
    ]
    for step in payload.get("steps") or []:
        if not isinstance(step, dict):
            continue
        name = str(step.get("step") or "")
        outcome = str(step.get("outcome") or "")
        line = str(step.get("line") or "")
        thinking = str(step.get("thinking") or "").strip()
        lines.append(f"Step {name} ({outcome}): {line}")
        if thinking:
            lines.append(f"Thinking during {name}: {thinking[:4000]}")
    history = payload.get("history") or []
    for turn in history[-8:]:
        if not isinstance(turn, dict):
            continue
        role = "User" if turn.get("role") == "user" else "Ledger"
        lines.append(f"{role}: {str(turn.get('text') or '')[:1000]}")
    text = "\n".join(line for line in lines if line.strip())
    return text[:12000]


def _prompt(question: str, context: str) -> str:
    schema = json.dumps(ASK_SCHEMA)
    return (
        "You are Ledger, answering a question about one invoice run. "
        "Use only the run below. If the run does not say, say that you cannot tell. "
        "Do not claim you changed the books. "
        "thinking is what you checked, in a few sentences. "
        "answer is the reply. "
        f"Reply with one JSON object matching this schema:\n{schema}\n\n"
        f"RUN:\n{context}\n\n"
        f"QUESTION:\n{question}"
    )
