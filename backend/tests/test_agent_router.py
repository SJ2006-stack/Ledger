from pathlib import Path

from app.agent_router import plan_actions
from app.config import Settings
from app.llm import JsonReply


def _settings(*, key: str = "", extractor: str = "template", channel: str = "") -> Settings:
    return Settings(
        groq_api_key=key,
        model="qwen/qwen3.8-27b",
        extractor=extractor,
        db_path=Path("/tmp/unused.db"),
        slack_default_channel=channel,
    )


def test_keywords_process_invoice_when_file_attached():
    steps = plan_actions(
        "Please process this invoice in the workbook",
        has_file=True,
        has_result=False,
        settings=_settings(),
    )
    assert steps[0]["tool"] == "process_invoice"


def test_keywords_ask_for_file_when_missing():
    steps = plan_actions(
        "Process the invoice",
        has_file=False,
        has_result=False,
        settings=_settings(),
    )
    assert steps[0]["tool"] == "ask"
    assert "attach" in steps[0]["question"].casefold()


def test_guard_blocks_email_without_recipient():
    steps = plan_actions(
        "Email the result",
        has_file=False,
        has_result=True,
        settings=_settings(),
    )
    assert steps[0]["tool"] == "ask"
    assert "email" in steps[0]["question"].casefold()


def test_guard_accepts_email_with_address():
    steps = plan_actions(
        "Email the result to ap@example.com",
        has_file=False,
        has_result=True,
        settings=_settings(),
    )
    assert any(step["tool"] == "send_email" and step["to"] == "ap@example.com" for step in steps)


def test_slack_without_a_named_channel_uses_general():
    steps = plan_actions(
        "Post this to Slack",
        has_file=False,
        has_result=True,
        settings=_settings(),
    )
    assert steps[0]["tool"] == "post_slack"
    assert steps[0]["channel"] == "general"


def test_model_plan_is_used_when_groq_configured(monkeypatch):
    def fake_generate(api_key, model, prompt, *, max_tokens=4096):
        del api_key, model, prompt, max_tokens
        return JsonReply(
            payload={
                "steps": [
                    {"tool": "process_invoice", "to": "", "channel": "", "note": "", "question": ""},
                    {"tool": "reply", "to": "", "channel": "", "note": "", "question": ""},
                ]
            },
            text="{}",
            input_tokens=1,
            output_tokens=2,
            source="groq_json",
        )

    monkeypatch.setattr("app.agent_router.generate_json", fake_generate)
    steps = plan_actions(
        "handle this",
        has_file=True,
        has_result=False,
        settings=_settings(key="k", extractor="groq"),
    )
    assert steps[0]["tool"] == "process_invoice"


def test_attached_file_is_processed_when_the_model_only_asks(monkeypatch):
    def fake_generate(api_key, model, prompt, *, max_tokens=4096):
        del api_key, model, prompt, max_tokens
        return JsonReply(
            payload={
                "steps": [
                    {
                        "tool": "ask",
                        "to": "",
                        "channel": "",
                        "note": "",
                        "question": "Which email recipient and Slack channel should I use?",
                    }
                ]
            },
            text="{}",
            input_tokens=1,
            output_tokens=2,
            source="groq_json",
        )

    monkeypatch.setattr("app.agent_router.generate_json", fake_generate)
    steps = plan_actions(
        "look after these invoices",
        has_file=True,
        has_result=False,
        settings=_settings(key="k", extractor="groq"),
    )
    assert steps[0]["tool"] == "process_invoice"
    assert all(step["tool"] != "ask" for step in steps)


def test_current_ledger_mail_does_not_ask_for_a_file(monkeypatch):
    def boom(*args, **kwargs):
        del args, kwargs
        raise AssertionError("the model should not plan a ledger-folder email")

    monkeypatch.setattr("app.agent_router.generate_json", boom)
    steps = plan_actions(
        "mail the current ledger folder on my email shriyanshjaiswl.xf@gmail.com",
        has_file=False,
        has_result=False,
        settings=_settings(key="k", extractor="groq"),
    )
    assert steps[0]["tool"] == "send_ledger"
    assert steps[0]["to"] == "shriyanshjaiswl.xf@gmail.com"
    assert all(step["tool"] not in {"ask", "process_invoice"} for step in steps)


def test_ledger_mail_without_an_address_asks_and_does_not_send():
    steps = plan_actions(
        "mail the current ledger folder",
        has_file=False,
        has_result=False,
        settings=_settings(),
    )
    assert steps == [
        {
            "tool": "ask",
            "to": "",
            "channel": "",
            "note": "",
            "question": "Who should I email?",
        }
    ]


def test_model_failure_falls_back_to_keywords(monkeypatch):
    def boom(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("groq down")

    monkeypatch.setattr("app.agent_router.generate_json", boom)
    steps = plan_actions(
        "Please process this invoice",
        has_file=True,
        has_result=False,
        settings=_settings(key="k", extractor="groq"),
    )
    assert steps[0]["tool"] == "process_invoice"
