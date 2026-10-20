import pytest

from app.llm import JsonReply, _loads_object, generate_json, is_transient


def test_loads_plain_json_object():
    assert _loads_object('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_loads_fenced_json_block():
    text = """```json
{"vendor_name": "Helios", "total": 5900}
```"""
    assert _loads_object(text) == {"vendor_name": "Helios", "total": 5900}


def test_loads_rejects_array_and_garbage():
    assert _loads_object("[1, 2]") is None
    assert _loads_object("not json") is None
    assert _loads_object("") is None
    assert _loads_object("   ") is None


def test_is_transient_covers_rate_limits_and_server_errors():
    assert is_transient(RuntimeError("Error 429 rate limit"))
    assert is_transient(RuntimeError("500 INTERNAL"))
    assert is_transient(RuntimeError("service UNAVAILABLE"))
    assert is_transient(RuntimeError("OVERLOADED"))
    assert not is_transient(RuntimeError("400 INVALID_ARGUMENT"))


def test_generate_json_parses_mock_completion(monkeypatch):
    class _Usage:
        prompt_tokens = 11
        completion_tokens = 22

    class _Message:
        content = '```json\n{"ok": true, "n": 3}\n```'

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Completion:
        choices = [_Choice()]
        usage = _Usage()

    class _Completions:
        def create(self, **kwargs):
            assert kwargs["response_format"] == {"type": "json_object"}
            assert kwargs["model"] == "qwen/qwen3.8-27b"
            return _Completion()

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, api_key: str) -> None:
            assert api_key == "test-key"
            self.chat = _Chat()

    monkeypatch.setattr("app.llm.Groq", _Client)
    reply = generate_json("test-key", "qwen/qwen3.8-27b", "say ok")
    assert isinstance(reply, JsonReply)
    assert reply.payload == {"ok": True, "n": 3}
    assert reply.input_tokens == 11
    assert reply.output_tokens == 22
    assert reply.source == "groq_json"


def test_generate_json_raises_when_content_is_not_an_object(monkeypatch):
    class _Message:
        content = "[1, 2, 3]"

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Completion:
        choices = [_Choice()]
        usage = None

    class _Completions:
        def create(self, **kwargs):
            del kwargs
            return _Completion()

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, api_key: str) -> None:
            del api_key
            self.chat = _Chat()

    monkeypatch.setattr("app.llm.Groq", _Client)
    with pytest.raises(ValueError, match="no JSON object"):
        generate_json("k", "m", "prompt")
