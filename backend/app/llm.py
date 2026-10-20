"""Shared Groq chat helper for JSON replies."""

from __future__ import annotations

import json
from dataclasses import dataclass

from groq import Groq


@dataclass(frozen=True)
class JsonReply:
    payload: dict
    text: str
    input_tokens: int
    output_tokens: int
    source: str


def generate_json(
    api_key: str,
    model: str,
    prompt: str,
    *,
    max_tokens: int = 4096,
) -> JsonReply:
    client = Groq(api_key=api_key)
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    choice = completion.choices[0] if completion.choices else None
    text = (getattr(getattr(choice, "message", None), "content", None) or "").strip()
    usage = completion.usage
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    payload = _loads_object(text)
    if payload is None:
        raise ValueError(f"Model returned no JSON object. {text[:240]}")
    return JsonReply(
        payload=payload,
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        source="groq_json",
    )


def _loads_object(text: str) -> dict | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def is_transient(exc: Exception) -> bool:
    text = str(exc).casefold()
    markers = ("429", "500", "502", "503", "rate", "timeout", "temporar", "unavailable", "overloaded")
    return any(marker in text for marker in markers)
